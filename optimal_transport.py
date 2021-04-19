import ot
import os
import gzip
import pickle
import random
import time
import numpy as np
import xgboost as xgb
from sklearn.metrics import average_precision_score
import itertools
# tool to debug
from icecream import ic


def objective_AP(preds, dtrain):
    labels = dtrain.get_label()
    preds = 1.0 / (1.0 + np.exp(-preds))
    dsig = preds * (1 - preds)
    sum_pos = np.sum(preds[labels == 1])
    sum_neg = np.sum(preds[labels != 1])
    sum_tot = sum(preds)
    grad = ((labels == 1) * (-1) * sum_neg * dsig +
            (labels != 1) * dsig * sum_pos) / (sum_tot)
    hess = np.ones(len(preds)) * 0.1
    return grad, hess


def evalerror_AP(preds, dtrain):
    labels = dtrain.get_label()
    return 'AP', average_precision_score(labels, preds)


def predict_label(param, X_train, y_train, X_eval, algo='XGBoost'):
    if algo == 'XGBoost':
        d_train = xgb.DMatrix(X_train, label=y_train)
        d_eval = xgb.DMatrix(X_eval)

        evallist = [(d_train, 'train')]
        bst = xgb.train(param, d_train, param['num_boost_round'],
                        evallist, maximize=True,
                        early_stopping_rounds=50,
                        obj=objective_AP,
                        feval=evalerror_AP,
                        verbose_eval=False)
        prediction = bst.predict(d_eval)

        # TODO check the validity of this method !!
        labels = np.array(prediction) > 0.5
        labels = labels.astype(int)

        return labels


def ot_adaptation(X_source, y_source, X_target, param_ot, transpose=False):
    """
    Function computes the transport plan and transport the sources to the targets
    or the reverse
    :param param_ot:
    :param param_model:
    :param X_source: Source features
    :param y_source: Source labels
    :param X_target: Target features
    :param transpose: boolean set by default to False (transport sources to targets)
    if boolean is set to True the X_target is transported in the Source domain
    :return: Return the source features transported into the target if target_to_source = False
            Return the target features transported into the source if target_to_source = True
    """
    transport = ot.da.SinkhornLpl1Transport(reg_e=param_ot['reg_e'], reg_cl=param_ot['reg_cl'], norm="median")
    transport.fit(Xs=X_source, ys=y_source, Xt=X_target)
    if not transpose:
        transp_Xs = transport.transform(Xs=X_source)
        return transp_Xs
    else:
        transp_Xt = transport.inverse_transform(Xt=X_target)
        return transp_Xt


def uot_adaptation(X_source, y_source, X_target, param_ot, target_to_source=False):
    # https://pythonot.github.io/_modules/ot/da.html#SinkhornLpl1Transport
    # https://pythonot.github.io/gen_modules/ot.unbalanced.html
    # https://pythonot.github.io/_modules/ot/unbalanced.html#sinkhorn_knopp_unbalanced

    transport = ot.da.UnbalancedSinkhornTransport(reg_e=param_ot['reg_e'], reg_m=param_ot['reg_m'])
    # default use sinkhorn_knopp_unbalanced
    transport.fit(Xs=X_source, ys=y_source, Xt=X_target)
    if not target_to_source:
        transp_Xs = transport.transform(Xs=X_source)
        return transp_Xs
    else:
        transp_Xt = transport.inverse_transform(Xt=X_target)
        return transp_Xt


def generateSubset2(X, Y, p):
    """
    This function should not be used on target true label because the proportion of classes are not available.
    :param X: Features
    :param Y: Labels
    :param p: Percentage of data kept.
    :return: Subset of X and Y with same proportion of classes.
    """
    idx = []
    for c in np.unique(Y):
        idxClass = np.argwhere(Y == c).ravel()
        random.shuffle(idxClass)
        idx.extend(idxClass[0:int(p * len(idxClass))])
    return X[idx], Y[idx]


# take a grid of parameters in input and return all the possible combination (to avoid repeating the same test)
def create_grid_search_ot(params: dict):
    '''
    :param params: a dictionary containing the name of the parameters as keys and an array of their possible values
                    as values
    :return: all the possible combination of the values
    '''
    list_keys = list(params.keys())
    list_values = params.values()
    possible_combination_values = list(itertools.product(*list_values))

    possible_combination = []
    for values in possible_combination_values:
        temp_dico = dict()
        for i in range(len(list_keys)):
            key = list_keys[i]
            temp_dico[key] = values[i]
        possible_combination.append(temp_dico)

    return possible_combination


def ot_cross_validation(X_source, y_source, X_target, param_model, param_to_cross_valid,
                        transpose_plan=True, ot_type="UOT",
                        duration_max=24, nb_training_iteration=10, gridsearch=True):
    """
    find the best hyperparameters for an optimal transport
    :param X_source:
    :param y_source:
    :param X_target:
    :param param_model: parameters of the model (eg. XGBoost)
    :param param_to_cross_valid: dictionary of parameters we want to cross valid
    :param transpose_plan: True to project targets in Source, False otherwise (classic OT)
    :param ot_type: values can be "UOT" or "OT"
    :param duration_max: maximum running time
    :param nb_training_iteration:
    :param gridsearch: if True a GridSearch is done to tune the parameters, otherwise it RandomSearch
    :return:
    """
    max_iteration = 1000
    possible_param_combination = []

    if gridsearch:
        possible_param_combination = create_grid_search_ot(param_to_cross_valid)
        max_iteration = len(possible_param_combination)
        ic(len(possible_param_combination))

    param_train = dict([('reg_e', 0), ('reg_cl', 0)])
    time_start = time.time()
    nb_iteration = 0
    list_results = []

    while time.time() - time_start < 3600 * duration_max and nb_iteration < max_iteration:
        np.random.seed(4896 * nb_iteration + 5272)
        # TODO generalize so that if need to cross validate more parameters
        #  we won't have to rewrite the code
        if gridsearch and len(possible_param_combination) > 0:
            param_train['reg_e'] = possible_param_combination[nb_iteration]['reg_e']
            param_train['reg_cl'] = possible_param_combination[nb_iteration]['reg_cl']
        else:  # Random search
            param_train['reg_e'] = param_to_cross_valid['reg_e'][np.random.randint(len(param_to_cross_valid['reg_e']))]
            param_train['reg_cl'] = param_to_cross_valid['reg_cl'][np.random.randint(len(param_to_cross_valid['reg_cl']))]
        try:
            for i in range(nb_training_iteration):
                ic(param_train)
                # if we want to project the targets in the Source domain
                if transpose_plan:
                    # Do the first adaptation (from source to target for the plan but adapt with the transpose)
                    if ot_type == "OT":
                        trans_X_target = ot_adaptation(X_source, y_source, X_target, param_train, transpose=True)
                    else: # Unbalanced OT
                        trans_X_target = uot_adaptation(X_source, y_source, X_target, param_train, target_to_source=True)

                    # Get pseudo labels
                    trans_pseudo_y_target = predict_label(param_model, X_source, y_source, trans_X_target)

                    # Do the second adaptation (from target to source)
                    # We don't use target_to_source = True, instead we reverse the target and source in parameters
                    # bc we don't want to use the transpose of a plan here, just create a plan from Target to Source
                    # trans2_X_target = ot_adaptation(trans_X_target, trans_pseudo_y_target, X_source, param_train)
                    if ot_type == "OT":
                        trans2_X_target = ot_adaptation(X_target, trans_pseudo_y_target, X_source, param_train)
                    else:  # Unbalanced OT
                        trans2_X_target = uot_adaptation(X_target, trans_pseudo_y_target, X_source, param_train)

                    for j in range(10):
                        ic()
                        subset_trans2_X_target, subset_trans_pseudo_y_target = generateSubset2(trans2_X_target,
                                                                                               trans_pseudo_y_target,
                                                                                               p=0.5)
                        # ic(subset_trans2_X_target)
                        # ic(subset_trans_pseudo_y_target)
                        y_source_pred = predict_label(param_model,
                                                      subset_trans2_X_target,
                                                      subset_trans_pseudo_y_target,
                                                      X_source)
                        precision = 100 * float(sum(y_source_pred == y_source)) / len(y_source_pred)
                        average_precision = 100 * average_precision_score(y_source, y_source_pred)

                    # add results + param for this loop to the pickle
                    to_save = dict(param_train)
                    to_save['precision'] = precision
                    to_save['average_precision'] = average_precision
                    list_results.append(to_save)
                # if we want to project the sources in the Target domain (classic method)
                else :
                    # First adaptation
                    if ot_type == "OT":
                        trans_X_source = ot_adaptation(X_source, y_source, X_target, param_train,
                                                       transpose=False)
                    else:
                         trans_X_source = uot_adaptation(X_source, y_source, X_target, param_train,
                                                        target_to_source=False)
                    # Get pseudo labels
                    trans_pseudo_y_source = predict_label(param_model, trans_X_source, y_source, X_target)

                    # Second adaptation
                    if ot_type == "OT":
                        trans2_X_target = ot_adaptation(X_source=X_target, y_source=trans_pseudo_y_source,
                                                        X_target=X_source, param_ot=param_train,
                                                        transpose=False)
                    else:  # Unbalanced OT
                        trans2_X_target = uot_adaptation(X_source=X_target, y_source=trans_pseudo_y_source,
                                                            X_target=X_source, param_ot=param_train,
                                                            target_to_source=False)

                    for j in range(10):
                        ic()
                        subset_trans2_X_target, subset_trans_pseudo_y_target = generateSubset2(trans2_X_target,
                                                                                               trans_pseudo_y_source,
                                                                                               p=0.5)
                        # ic(subset_trans2_X_target)
                        # ic(subset_trans_pseudo_y_target)
                        y_source_pred = predict_label(param_model,
                                                      subset_trans2_X_target,
                                                      subset_trans_pseudo_y_target,
                                                      X_source)
                        precision = 100 * float(sum(y_source_pred == y_source)) / len(y_source_pred)
                        average_precision = 100 * average_precision_score(y_source, y_source_pred)

                    # add results + param for this loop to the pickle
                    to_save = dict(param_train)
                    to_save['precision'] = precision
                    to_save['average_precision'] = average_precision
                    list_results.append(to_save)
        except Exception as e:
            ic()
            print("Exception in transfer_cross_validation_trg_to_src", e)
        time.sleep(1.)  # Allow us to stop the program with ctrl-C
        nb_iteration += 1
        if to_save:
            ic(nb_iteration, to_save)
        else:
            ic(nb_iteration)

    optimal_param = max(list_results, key=lambda val: val['average_precision'])
    return optimal_param
