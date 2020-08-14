# Copyright (c) Facebook, Inc. and its affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
#

import numpy as np
import torch
import math

from sklearn.linear_model import LinearRegression
from itertools import chain, combinations
from scipy.stats import f as fdist
from scipy.stats import ttest_ind

from torch.autograd import grad

import scipy.optimize

import matplotlib
import matplotlib.pyplot as plt


def pretty(vector):
    vlist = vector.view(-1).tolist()
    return "[" + ", ".join("{:+.4f}".format(vi) for vi in vlist) + "]"


class InvariantRiskMinimization(object):
    def __init__(self, environments, args):
        best_reg = 0
        best_err = 1e6

        x_val = environments[-1][0]
        y_val = environments[-1][1]

        for reg in [0, 1e-5, 1e-4, 1e-3, 1e-2, 1e-1]:
            self.train(environments[:-1], args, reg=reg)
            err = (x_val @ self.solution() - y_val).pow(2).mean().item()

            if args["verbose"]:
                print("IRM (reg={:.3f}) has {:.3f} validation error.".format(
                    reg, err))

            if err < best_err:
                best_err = err
                best_reg = reg
                best_phi = self.phi.clone()
        self.phi = best_phi


    def train(self, environments, args, reg=0):
        dim_x = environments[0][0].size(1)

        use_cuda = torch.cuda.is_available()
        device = torch.device("cuda" if use_cuda else "cpu")

        self.phi = torch.nn.Parameter(torch.eye(dim_x, dim_x, device = device))
        self.w = torch.ones(dim_x, 1, requires_grad = True, device = device)

        opt = torch.optim.Adam([self.phi], lr=args["lr"])
        loss = torch.nn.MSELoss()

        for iteration in range(args["n_iterations"]):
            penalty = 0
            error = 0
            for x_e, y_e in environments:
                x_e, y_e = x_e.to(device), y_e.to(device)
                error_e = loss(x_e @ self.phi @ self.w, y_e)
                penalty += grad(error_e, self.w,
                                create_graph=True)[0].pow(2).mean()
                error += error_e

            opt.zero_grad()
            (reg * error + (1 - reg) * penalty).backward()
            opt.step()

            if args["verbose"] and iteration % 1000 == 0:
                w_str = pretty(self.solution())
                print("{:05d} | {:.5f} | {:.5f} | {:.5f} | {}".format(iteration,
                                                                      reg,
                                                                      error,
                                                                      penalty,
                                                                      w_str))

    def solution(self):
        return (self.phi @ self.w).to('cpu')


class InvariantCausalPrediction(object):
    def __init__(self, environments, args):
        self.coefficients = None
        self.alpha = args["alpha"]

        x_all = []
        y_all = []
        e_all = []

        for e, (x, y) in enumerate(environments):
            x_all.append(x.numpy())
            y_all.append(y.numpy())
            e_all.append(np.full(x.shape[0], e))

        x_all = np.vstack(x_all)
        y_all = np.vstack(y_all)
        e_all = np.hstack(e_all)

        dim = x_all.shape[1]

        accepted_subsets = []
        for subset in self.powerset(range(dim)):
            if len(subset) == 0:
                continue

            x_s = x_all[:, subset]
            reg = LinearRegression(fit_intercept=False).fit(x_s, y_all)

            p_values = []
            for e in range(len(environments)):
                e_in = np.where(e_all == e)[0]
                e_out = np.where(e_all != e)[0]

                res_in = (y_all[e_in] - reg.predict(x_s[e_in, :])).ravel()
                res_out = (y_all[e_out] - reg.predict(x_s[e_out, :])).ravel()

                p_values.append(self.mean_var_test(res_in, res_out))

            # TODO: Jonas uses "min(p_values) * len(environments) - 1"
            p_value = min(p_values) * len(environments)

            if p_value > self.alpha:
                accepted_subsets.append(set(subset))
                if args["verbose"]:
                    print("Accepted subset:", subset)

        if len(accepted_subsets):
            accepted_features = list(set.intersection(*accepted_subsets))
            if args["verbose"]:
                print("Intersection:", accepted_features)
            self.coefficients = np.zeros(dim)

            if len(accepted_features):
                x_s = x_all[:, list(accepted_features)]
                reg = LinearRegression(fit_intercept=False).fit(x_s, y_all)
                self.coefficients[list(accepted_features)] = reg.coef_

            self.coefficients = torch.Tensor(self.coefficients)
        else:
            self.coefficients = torch.zeros(dim)

    def mean_var_test(self, x, y):
        pvalue_mean = ttest_ind(x, y, equal_var=False).pvalue
        pvalue_var1 = 1 - fdist.cdf(np.var(x, ddof=1) / np.var(y, ddof=1),
                                    x.shape[0] - 1,
                                    y.shape[0] - 1)

        pvalue_var2 = 2 * min(pvalue_var1, 1 - pvalue_var1)

        return 2 * min(pvalue_mean, pvalue_var2)

    def powerset(self, s):
        return chain.from_iterable(combinations(s, r) for r in range(len(s) + 1))

    def solution(self):
        return self.coefficients


class EmpiricalRiskMinimizer(object):
    def __init__(self, environments, args):
        x_all = torch.cat([x for (x, y) in environments]).numpy()
        y_all = torch.cat([y for (x, y) in environments]).numpy()

        w = LinearRegression(fit_intercept=False).fit(x_all, y_all).coef_
        self.w = torch.Tensor(w)

    def solution(self):
        return self.w

class EnsembleERM(object):
    def __init__(self, environments, args):
        
        x_all = torch.cat([x for (x, y) in environments]).numpy()
        y_all = torch.cat([y for (x, y) in environments]).numpy()
        w = LinearRegression(fit_intercept=False).fit(x_all, y_all).coef_ * 0.0

        for (x_e, y_e) in environments:
            w_e = LinearRegression(fit_intercept=False).fit(x_e.numpy(), y_e.numpy()).coef_
            w += w_e
        w /= len(environments)
        self.w = torch.Tensor(w)

    def solution(self):
        return self.w

class AdaBoostERM(object):
    def __init__(self, environments, args):
        num_classifiers = 10
        classifiers = []
        classifier_weights = np.ones(num_classifiers)/num_classifiers

        env_weights = np.ones(len(environments))/len(environments)
        env_errors = np.zeros(len(environments))

        x_all = torch.cat([x for (x, y) in environments]).numpy()
        y_all = torch.cat([y for (x, y) in environments]).numpy()
        for i in range(num_classifiers):
            if i == 0:
                w_average = LinearRegression(fit_intercept=False).fit(x_all, y_all).coef_ * 0.0

                for (x_e, y_e) in environments:
                    w_e = LinearRegression(fit_intercept=False).fit(x_e.numpy(), y_e.numpy()).coef_
                    w_average += w_e
                w_average /= len(environments)

                lr_i = LinearRegression(fit_intercept=False).fit(x_all, y_all)
                lr_i.coef_ = w_average
            else:
                e = np.random.choice(len(environments), p=env_weights)
                (x_e, y_e) = environments[e]
                lr_i = LinearRegression(fit_intercept=False).fit(x_e.numpy(), y_e.numpy())
            
            for e, (x_e, y_e) in enumerate(environments):
                y_hat_e = lr_i.predict(x_e.numpy())
                env_errors[e] = np.mean(np.abs(y_hat_e - y_e.numpy())**2)

            env_errors /= np.max(env_errors)
            avg_error = np.mean(env_errors)
            beta = (1 - avg_error)/avg_error
            classifier_weights[i] = np.log(beta)/2.0

            env_weights = np.multiply(env_weights, np.power(beta, 1 - env_errors))
            env_weights /= np.max(env_weights)
            print(env_weights)

            classifiers += [lr_i]

        w = LinearRegression(fit_intercept=False).fit(x_all, y_all).coef_ * 0.0

        for i in range(num_classifiers):
            w += classifier_weights[i]*classifiers[i].coef_
        w_average /= sum(classifier_weights)

        self.w = torch.Tensor(w)

    def solution(self):
        return self.w