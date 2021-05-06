import csv
import os
import gzip
import random
import pickle
import time
import numpy as np
import xgboost as xgb
from sys import argv, stdout, stderr
from threading import Thread
import pandas as pd
from datetime import datetime
from sklearn import preprocessing
from sklearn.impute import SimpleImputer, KNNImputer
import pathlib
from icecream import ic
from sklearn.metrics import average_precision_score
from sklearn.model_selection import train_test_split, StratifiedKFold

from baselines import components_analysis_based_method_cross_validation, sa_adaptation, coral_adaptation, tca_adaptation
from optimal_transport import evalerror_AP, objective_AP, ot_cross_validation, uot_adaptation, jcpot_adaptation, \
    reweighted_uot_adaptation, ot_adaptation


def import_dataset(filename, select_feature=True):
    data = pd.read_csv(filename, index_col=False).drop('index', axis='columns')

    if select_feature:
        data = feature_selection(data)

    y = data.loc[:, 'y'].to_numpy()
    X = data.loc[:, data.columns != 'y'].to_numpy()

    # data = pd.read_csv(filename, index_col=False).drop('index', axis='columns')
    # data.columns = range(data.shape[1])

    # y = data.loc[:, len(data.columns)-1]
    # X = data.loc[:, 0:len(data.columns)-2]
    X = set_nan_to_zero(X)
    return X, y


def feature_selection(dataframe):
    for column_name in dataframe.columns:
        if "rto" in column_name or "ecart" in column_name or "elast" in column_name:
            dataframe = dataframe.drop(column_name, axis='columns')
    return dataframe


def get_normalizer_data(data, type):
    if type == "Standard":
        return preprocessing.StandardScaler().fit(data)
    elif type == "Normalizer":
        normalizer = get_normalizer(data)
        return normalizer
    elif type == "Outliers_Robust":
        return preprocessing.RobustScaler().fit(data)


def get_normalizer(X, norm='l2'):
    if norm == 'l1':
        normalizer = np.abs(X).sum(axis=1)
    else:
        normalizer = np.einsum('ij,ij->i', X, X)
        np.sqrt(normalizer, normalizer)
    return normalizer


def normalize(X, normalizer, inverse):
    if not inverse:
        for i in range(X.shape[1]):
            X[:, i] = X[:, i] / normalizer[i]
    else:
        for i in range(X.shape[1]):
            X[:, i] = X[:, i] * normalizer[i]
    return X


def reweight_preprocessing(X, coefficients):
    """
    reweight the data depending on how much impacted they are by the degradation
    :param X: dataset without nan values
    :param coefficients: array of the degradation coefficients
    :return: reweighted dataset
    """
    reweighted_X = np.array([])
    for i in range(X.shape[1]):
        weight = 1 - coefficients[i] + 0.1
        reweighted_X = np.append(reweighted_X, weight * X[i])
    return reweighted_X


# set_nan_to_zero must be used using the name of the features => must be called during import_dataset
# (before transformation of the dataset to numpy)
def set_nan_to_zero(arr):
    imputer = SimpleImputer(missing_values=np.nan, strategy="constant", fill_value=1e-5)
    imputer.fit(arr)
    arr = imputer.transform(arr)
    # to avoid true divide by 0
    arr = np.where(arr == 0, 1e-5, arr)
    return arr


def fill_nan(arr, strategy='mean', fill_value=0, n_neighbors=5):
    """
    Replace NaN values in arrays (to be used notably for PCA)
    :param arr: array containing NaN values
    :param strategy: strategy to use to replace the values, can be 'mean', "median", "most_frequent" or "constant"
    :param fill_value: value used if constant is chosen
    :return:
    """
    if strategy == "knn":
        imputer = KNNImputer(missing_values=np.nan, n_neighbors=n_neighbors)
    else:
        imputer = SimpleImputer(missing_values=np.nan, strategy=strategy, fill_value=fill_value)
    imputer.fit(arr)
    return imputer.transform(arr)


def load_csv(path):
    data = []
    with open(path, 'r') as csvfile:
        reader = csv.reader(csvfile, delimiter=',')
        for row in reader:
            data.append(np.array(row))
    data = np.array(data)
    n = 0
    d = 0
    try:
        (n, d) = data.shape
    except ValueError:
        pass
    return data, n, d


def parse_value_from_cvs(value):
    try:
        return int(value)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return value  # the value is a string


def import_hyperparameters(algo: str, filename="hyperparameters.csv", toy_example=False):
    """
    :param filename:
    :param algo: name of the algorithm we want the hyperparameters of
    :return: a dictionary of hyperparameters
    """
    if not toy_example:
        algo = 'ap'

    imported_csv_content = pd.read_csv(filename, delimiter=";")
    to_return = dict()
    column = imported_csv_content[algo]
    for i in range(len(column)):
        key, value = imported_csv_content[algo][i].split(",")
        if key != "eval_metric":
            to_return[key] = parse_value_from_cvs(value)
    return to_return


def export_hyperparameters(algo, hyperparameters, filename="hyperparameters.csv"):
    """
    :param filename:
    :param algo: name of the algo (str)
    :param hyperparameters: a dictionary of parameters we want to save
    :return:
    """
    list_hyperparam = []
    for key in hyperparameters.keys():
        list_hyperparam.append((key + "," + str(hyperparameters[key])))
    try:
        hyperparameters_dataset = pd.read_csv(filename, delimiter=";")
        hyperparameters_dataset[algo] = list_hyperparam
    except FileNotFoundError:
        hyperparameters_dataset = pd.DataFrame(columns=[algo], data=list_hyperparam)
    hyperparameters_dataset.to_csv(filename, index=False)


def data_recovery(dataset):
    # Depending on the value on the 8th columns, the label is attributed
    # then we can have three differents datasets :
    # - abalone8
    # - abalone17
    # - abalone20
    if dataset in ['abalone8', 'abalone17', 'abalone20']:
        data = pd.read_csv("datasets/abalone.data", header=None)
        data = pd.get_dummies(data, dtype=float)
        if dataset in ['abalone8']:
            y = np.array([1 if elt == 8 else 0 for elt in data[8]])
        elif dataset in ['abalone17']:
            y = np.array([1 if elt == 17 else 0 for elt in data[8]])
        elif dataset in ['abalone20']:
            y = np.array([1 if elt == 20 else 0 for elt in data[8]])
        X = np.array(data.drop([8], axis=1))
    elif dataset in ['satimage']:
        data, n, d = load_csv('datasets/satimage.data')
        X = data[:, np.arange(d - 1)].astype(float)
        y = data[:, d - 1]
        y = y.astype(int)
        y[y != 4] = 0
        y[y == 4] = 1
    return X, y


# Create grid of parameters given parameters ranges
def listP(dic):
    params = list(dic.keys())
    listParam = [{params[0]: value} for value in dic[params[0]]]
    for i in range(1, len(params)):
        newListParam = []
        currentParamName = params[i]
        currentParamRange = dic[currentParamName]
        for previousParam in listParam:
            for value in currentParamRange:
                newParam = previousParam.copy()
                newParam[currentParamName] = value
                newListParam.append(newParam)
        listParam = newListParam.copy()
    return listParam


def applyAlgo(algo, p, Xtrain, ytrain, Xtest, ytest, Xtarget, ytarget, Xclean):
    if algo == 'XGBoost':
        dtrain = xgb.DMatrix(Xtrain, label=ytrain)
        dtest = xgb.DMatrix(Xtest)
        dtarget = xgb.DMatrix(Xtarget)
        dclean = xgb.DMatrix(Xclean)
        evallist = [(dtrain, 'train')]
        # p = param
        bst = xgb.train(p, dtrain, p['num_round'],
                        evallist, maximize=True,
                        early_stopping_rounds=50,
                        obj=objective_AP,
                        feval=evalerror_AP,
                        verbose_eval=False)
        rankTrain = bst.predict(dtrain)
        rankTest = bst.predict(dtest)
        rankTarget = bst.predict(dtarget)
        rankClean = bst.predict(dclean)

        """predict_y = np.array(rankTarget) > 0.5
        predict_y = predict_y.astype(int)
        print(predict_y)

        print("precision", 100 * float(sum(predict_y == ytarget)) / len(predict_y))
        print("ap", average_precision_score(ytarget, rankTarget) * 100)"""
    return (average_precision_score(ytrain, rankTrain) * 100,
            average_precision_score(ytest, rankTest) * 100,
            average_precision_score(ytarget, rankClean) * 100,
            average_precision_score(ytarget, rankTarget) * 100)


def print_whole_repo(repo):
    for file in pathlib.Path(repo).iterdir():
        path = str(file)

        print_pickle(path, "results")
        print(" ")


def print_pickle(filename, type=""):
    if type == "results":
        print("Data saved in", filename)
        file = gzip.open(filename, 'rb')
        data = pickle.load(file)
        file.close()
        for dataset in data:
            for transport in data.get(dataset):
                results = data[dataset][transport]
                print("Dataset:", dataset, "Transport method :", transport, "Algo:", results[0],
                      "Train AP {:5.2f}".format(results[1]),
                      "Test AP {:5.2f}".format(results[2]),
                      "Clean AP {:5.2f}".format(results[3]),
                      "Target AP {:5.2f}".format(results[4]),
                      "Parameters:", results[7])
    elif type == "results_adapt":
        print("Data saved in", filename)
        file = gzip.open(filename, 'rb')
        data = pickle.load(file)
        file.close()
        for dataset in data:
            for transport in data.get(dataset):
                results = data[dataset][transport]
                print("Dataset:", dataset, "Transport method :", transport, "Algo:", results[0],
                      "Train AP {:5.2f}".format(results[1]),
                      "Test AP {:5.2f}".format(results[2]),
                      "Clean AP {:5.2f}".format(results[3]),
                      "Target AP {:5.2f}".format(results[4]),
                      "Parameters:", results[5],
                      "Parameters OT:", results[6])
    else:
        print("Data saved in", filename)
        file = gzip.open(filename, 'rb')
        data = pickle.load(file)
        file.close()
        print(data)


def pickle_to_latex(filenames, type=""):
    if type == "results":
        print("\\begin{table}[]\n\\begin{adjustbox}{max width=1.1\\textwidth,center}\n\\begin{tabular}{lllllll}",
              "\nDataset & Algorithme & Transport & Train AP & Test AP & Clean AP & ",
              "Target AP\\\\")
        for filename in filenames:
            file = gzip.open(filename, 'rb')
            data = pickle.load(file)
            file.close()
            for dataset in data:
                for transport in data.get(dataset):
                    results = data[dataset][transport]
                    print(dataset.replace("%", "\\%"), "&", results[0], "&", transport, "&",
                          "{:5.2f}".format(results[1]), "&",
                          "{:5.2f}".format(results[2]), "&",
                          "{:5.2f}".format(results[3]), "&", "{:5.2f}".format(results[4]),
                          "\\\\")
        print("""\\end{tabular}\n\\end{adjustbox}\n\\end{table}""")
    elif type == "results_adapt":
        print("\\begin{table}[]\n\\begin{adjustbox}{max width=1.1\\textwidth,center}\n\\begin{tabular}{llllllllll}",
              "\nDataset & Algorithme &  Transport & Train AP & Test AP & Clean AP & ",
              "Target AP & max\_depth & num\_boost\_round & param_OT \\\\")
        for filename in filenames:
            file = gzip.open(filename, 'rb')
            data = pickle.load(file)
            file.close()
            for dataset in data:
                for transport in data.get(dataset):
                    results = data[dataset][transport]
                    print(dataset.replace("%", "\\%"), "&", results[0], "&", transport, "&",
                          "{:5.2f}".format(results[1]), "&",
                          "{:5.2f}".format(results[2]), "&",
                          "{:5.2f}".format(results[3]), "&", "{:5.2f}".format(results[4]),
                          "&", "{:5.2f}".format(results[5]['max_depth']),
                          "&", "{:5.2f}".format(results[5]['num_round']),
                          "&", results[6], "\\\\")
        print("""\\end{tabular}\n\\end{adjustbox}\n\\end{table}""")


def cross_validation_model(filename="tuned_hyperparameters.csv"):
    listParams = {
        "XGBoost": listP(
            {'max_depth': range(1, 6),
             # 'eta': [10 ** (-i) for i in range(1, 5)],
             # 'subsample': np.arange(0.1, 1, 0.1),
             # 'colsample_bytree': np.arange(0.1, 1, 0.1),
             'gamma': range(0, 21),
             # 'num_boost_round': range(100, 1001, 100)
             })
    }

    nbFoldValid = 5
    seed = 1

    results = {}
    for dataset in ['abalone20', 'abalone17', 'satimage', 'abalone8']:  # ['abalone8']:  #
        X, y = data_recovery(dataset)
        dataset_name = dataset
        pctPos = 100 * len(y[y == 1]) / len(y)
        dataset = "{:05.2f}%".format(pctPos) + " " + dataset
        print(dataset)
        np.random.seed(seed)
        random.seed(seed)

        Xsource, Xtarget, ysource, ytarget = train_test_split(X, y, shuffle=True,
                                                              stratify=y,
                                                              test_size=0.51)
        # Keep a clean backup of Xtarget before degradation.
        Xclean = Xtarget.copy()
        # for loop -> degradation of the target
        # 3 features are deteriorated : the 2nd, the 3rd and the 4th
        for feat, coef in [(2, 0.1), (3, 10), (4, 0)]:
            # for features 2 and 3, their values are multiplied by a coefficient
            # resp. 0.1 and 10
            if coef != 0:
                Xtarget[:, feat] = Xtarget[:, feat] * coef
            # for feature 4, some of its values are (randomly) set to 0
            else:
                Xtarget[np.random.choice(len(Xtarget), int(len(Xtarget) / 2)),
                        feat] = 0

        # From the source, training and test set are created
        Xtrain, Xtest, ytrain, ytest = train_test_split(Xsource, ysource,
                                                        shuffle=True,
                                                        stratify=ysource,
                                                        test_size=0.3)

        # MODEL CROSS VALIDATION
        skf = StratifiedKFold(n_splits=nbFoldValid, shuffle=True)
        foldsTrainValid = list(skf.split(Xtrain, ytrain))
        results[dataset] = {}
        for algo in listParams.keys():
            start = time.time()
            if len(listParams[algo]) > 1:  # Cross validation
                validParam = []
                for param in listParams[algo]:
                    valid = []
                    for iFoldVal in range(nbFoldValid):
                        fTrain, fValid = foldsTrainValid[iFoldVal]
                        valid.append(applyAlgo(algo, param,
                                               Xtrain[fTrain], ytrain[fTrain],
                                               Xtrain[fValid], ytrain[fValid],
                                               Xtarget, ytarget, Xclean)[1])
                    validParam.append(np.mean(valid))
                param = listParams[algo][np.argmax(validParam)]
            else:  # No cross-validation
                param = listParams[algo][0]

            # LEARNING AND SAVING PARAMETERS
            apTrain, apTest, apClean, apTarget = applyAlgo(algo, param,
                                                           Xtrain, ytrain,
                                                           Xtest, ytest,
                                                           Xtarget, ytarget,
                                                           Xclean)
            results[dataset][algo] = (apTrain, apTest, apClean, apTarget, param)
            print(dataset, algo, "Train AP {:5.2f}".format(apTrain),
                  "Test AP {:5.2f}".format(apTest),
                  "Clean AP {:5.2f}".format(apClean),
                  "Target AP {:5.2f}".format(apTarget), param,
                  "in {:6.2f}s".format(time.time() - start))
        export_hyperparameters(dataset_name, param, filename)


def adaptation_cross_validation(Xsource, ysource, Xtarget, params_model, normalizer, rescale,
                                y_target=None, cv_with_true_labels=False,
                                nb_training_iteration=8,
                                transpose=True, adaptation="UOT"):
    if "OT" in adaptation:
        # we define the parameters to cross valid
        possible_reg_e = [0.001, 0.01, 0.05, 0.1, 0.5, 1, 2, 5]
        possible_reg_cl = [0.001, 0.01, 0.05, 0.1, 0.5, 1, 2, 5]
        possible_weighted_reg_m = [{"0": 2, "1": 1}, {"0": 5, "1": 1}, {"0": 10, "1": 1}, {"0": 20, "1": 1},
                                   {"0": 50, "1": 1}, {"0": 100, "1": 1}, {"0": 200, "1": 1}]

        if adaptation == "UOT":
            param_to_cv = {'reg_e': possible_reg_e, 'reg_m': possible_reg_cl}
        elif adaptation == "JCPOT":
            param_to_cv = {'reg_e': possible_reg_e}
        elif adaptation == "reweight_UOT":
            param_to_cv = {'reg_e': possible_reg_e, 'reg_m': possible_weighted_reg_m}
        else:  # OT
            param_to_cv = {'reg_e': possible_reg_e, 'reg_cl': possible_reg_cl}

        cross_val_result, cross_val_result_cheat = ot_cross_validation(Xsource, ysource, Xtarget, params_model,
                                                                       param_to_cv, normalizer,
                                                                       rescale,
                                                                       y_target=y_target,
                                                                       cv_with_true_labels=cv_with_true_labels,
                                                                       nb_training_iteration=nb_training_iteration,
                                                                       transpose_plan=transpose, ot_type=adaptation)
        if adaptation == "UOT":
            param_transport = {'reg_e': cross_val_result['reg_e'], 'reg_m': cross_val_result['reg_m']}
        elif adaptation == "JCPOT":
            param_transport = {'reg_e': cross_val_result['reg_e']}
        elif adaptation == "reweight_UOT":
            param_transport = {'reg_e': cross_val_result['reg_e'], 'reg_m': cross_val_result['reg_m']}
        else:  # OT
            param_transport = {'reg_e': cross_val_result['reg_e'], 'reg_cl': cross_val_result['reg_cl']}
        return param_transport, cross_val_result_cheat
    elif adaptation == "SA":
        return {'d': components_analysis_based_method_cross_validation(Xsource, ysource, Xtarget, params_model, rescale,
                                                                       normalizer, transport_type="SA",
                                                                       extended_CV=cv_with_true_labels)}, None
    elif adaptation == "CORAL":
        return dict()  # equivalent to null but avoid crash later
    elif adaptation == "TCA":
        return {'d': components_analysis_based_method_cross_validation(Xsource, ysource, Xtarget, params_model, rescale,
                                                                       normalizer, transport_type="TCA",
                                                                       extended_CV=cv_with_true_labels)}, None

def adapt_domain(Xsource, ysource, Xtarget, Xclean, param_transport, transpose, adaptation):
    if "OT" in adaptation:
        # Transport sources to Target
        if not transpose:
            if adaptation == "UOT":
                Xsource = uot_adaptation(Xsource, ysource, Xtarget, param_transport, transpose)
            elif adaptation == "JCPOT":
                Xsource = jcpot_adaptation(Xsource, ysource, Xtarget, param_transport, transpose)
            elif adaptation == "reweight_UOT":
                Xsource = reweighted_uot_adaptation(Xsource, ysource, Xtarget, param_transport, transpose)
            else:  # OT
                Xsource = ot_adaptation(Xsource, ysource, Xtarget, param_transport, transpose)
        # Unbalanced optimal transport targets to Source
        else:
            if adaptation == "UOT":
                Xtarget = uot_adaptation(Xsource, ysource, Xtarget, param_transport, transpose)
            elif adaptation == "JCPOT":
                Xtarget = jcpot_adaptation(Xsource, ysource, Xtarget, param_transport, transpose)
            elif adaptation == "reweight_UOT":
                Xtarget = reweighted_uot_adaptation(Xsource, ysource, Xtarget, param_transport, transpose)
            else:  # OT
                Xtarget = ot_adaptation(Xsource, ysource, Xtarget, param_transport, transpose)
    elif adaptation == "SA":
        original_Xsource = Xsource
        Xsource, Xtarget = sa_adaptation(Xsource, Xtarget, param_transport, transpose)
        # We have to do "adapt" Xclean because we work one subspace, by doing the following, we get the subspace of
        # Xclean but it is not adapted
        _, Xclean = sa_adaptation(original_Xsource, Xclean, param_transport, transpose=False)
    elif adaptation == "CORAL":
        # original_Xsource = Xsource
        Xsource, Xtarget = coral_adaptation(Xsource, Xtarget, transpose)
    elif adaptation == "TCA":
        # original_Xsource = Xsource
        # param_transport = {'d': 2}
        original_Xtarget = Xtarget
        Xsource, Xtarget = tca_adaptation(Xsource, Xtarget, param_transport)
        Xclean, _ = tca_adaptation(Xclean, original_Xtarget, param_transport)
    return Xsource, Xtarget, Xclean


def train_model(X_source, y_source, X_target, y_target, X_clean, params_model, normalizer, rescale, algo="XGBoost"):
    if rescale:
        ic(normalizer)
        X_source = normalize(X_source, normalizer, True)
        X_target = normalize(X_target, normalizer, True)
        X_clean = normalize(X_clean, normalizer, True)
        # TODO rearrange to be able to choose the normalizer
        """X_source = normalizer.inverse_transform(X_source)
        X_target = normalizer.inverse_transform(X_target)
        X_clean = normalizer.inverse_transform(X_clean)"""

    ic(X_source, X_target, X_clean)

    Xtrain, Xtest, ytrain, ytest = train_test_split(X_source, y_source,
                                                    shuffle=True,
                                                    stratify=y_source,
                                                    test_size=0.3)

    apTrain, apTest, apClean, apTarget = applyAlgo(algo, params_model,
                                                   Xtrain, ytrain,
                                                   Xtest, ytest,
                                                   X_target, y_target,
                                                   X_clean)

    return apTrain, apTest, apClean, apTarget


def save_results(adaptation, dataset, algo, apTrain, apTest, apClean, apTarget, params_model, param_transport, start,
                 filename, results, param_transport_true_labels=None):
    if param_transport_true_labels is None:
        param_transport_true_labels = {}
    results[dataset][adaptation] = (algo, apTrain, apTest, apClean, apTarget, params_model, param_transport,
                                    time.time() - start)

    print(dataset, algo, adaptation, "Train AP {:5.2f}".format(apTrain),
          "Test AP {:5.2f}".format(apTest),
          "Clean AP {:5.2f}".format(apClean),
          "Target AP {:5.2f}".format(apTarget), params_model, param_transport, param_transport_true_labels,
          "in {:6.2f}s".format(time.time() - start))

    if not os.path.exists("results"):
        try:
            os.makedirs("results")
        except:
            pass
    if filename == "":
        filename = f"./results/" + dataset + adaptation + algo + ".pklz"
    f = gzip.open(filename, "wb")
    pickle.dump(results, f)
    f.close()
    return results


def launch_run(dataset, source_path, target_path, hyperparameter_file, filename="", algo="XGBoost",
               adaptation_method="UOT", cv_with_true_labels=False, transpose=True, nb_iteration_cv=8,
               select_feature=True, nan_fill_strat='mean', nan_fill_constant=0, n_neighbors=20, rescale=True):
    """
    :param rescale:
    :param dataset: name of the dataset
    :param source_path: path to the cvs file containing the source dataset
    :param target_path: path to the cvs file containing the target dataset
    :param hyperparameter_file: path to the cvs file containing the hyperparameters of the model
    :param filename: name of the file where the results are exported, if "" a name is generated with the name of the
                    dataset, the model, the adaptation method and a unique id based on the launch time
    :param algo: learning model
    :param adaptation_method: adaptation technique : "UOT", "OT", "JCPOT", "reweight_UOT", "TCA", "SA", "CORAL", "NA"
    :param cv_with_true_labels: boolean, if True do the cross validation of the optimal transport
                                        using the true label of the target if available
    :param transpose: boolean, if True, transport the target examples in the Source domain
    :param nb_iteration_cv: nb of iteration to use in the cross validation of the adaptation
    :param select_feature:
    :param nan_fill_strat:
    :param nan_fill_constant:
    :param n_neighbors:
    :return:
    """
    X_source, y_source = import_dataset(source_path, select_feature)
    X_target, y_target = import_dataset(target_path, select_feature)
    ic(X_source, X_target)
    if rescale:
        normalizer = get_normalizer_data(X_source, "Normalizer")
        ic(normalizer)
        # normalizer = get_normalizer_data(X_source, "Outliers_Robust")
        X_source = normalize(X_source, normalizer, False)
        X_target = normalize(X_target, normalizer, False)
    else:
        normalizer = None
    X_clean = X_target
    ic(X_source, X_target, X_clean)

    params_model = import_hyperparameters(algo, hyperparameter_file)
    results = {}
    start = time.time()

    now = datetime.now()
    # create a repo per day to store the results => each repo has an id composed of the day and month
    repo_id = now.strftime("%d%m")
    file_id = now.strftime("%H%M%f")
    repo_name = "results" + repo_id + "/nuit"  # TODO remove
    if not os.path.exists(repo_name):
        try:
            os.makedirs(repo_name)
        except:
            pass

    results[dataset] = {}
    param_transport_true_label = {}
    if adaptation_method != "NA":
        if adaptation_method in {"SA", "CORAL", "TCA"}:
            if not select_feature:
                X_source = fill_nan(X_source, nan_fill_strat, n_neighbors)
                X_target = fill_nan(X_target, nan_fill_strat, n_neighbors)
                X_clean = X_target

        param_transport, param_transport_true_label = adaptation_cross_validation(X_source, y_source, X_target,
                                                                                  params_model, normalizer,
                                                                                  rescale, y_target=y_target,
                                                                                  cv_with_true_labels=cv_with_true_labels,
                                                                                  transpose=transpose,
                                                                                  adaptation=adaptation_method,
                                                                                  nb_training_iteration=nb_iteration_cv)

        X_source, X_target, X_clean = adapt_domain(X_source, y_source, X_target, X_clean, param_transport,
                                                   transpose, adaptation_method)
    else:
        param_transport = {}  # for the pickle

    # Creation of the filename
    if filename == "":
        if rescale:
            filename = f"./" + repo_name + "/" + dataset + "_rescale_" + adaptation_method + "_" + algo + file_id
        else:
            filename = f"./" + repo_name + "/" + dataset + "_" + adaptation_method + "_" + algo + file_id
    if adaptation_method in {"SA", "CORAL", "TCA"}:
        if not select_feature:
            ic()
            if nan_fill_strat == "constant":
                param_transport['nan_fill'] = {nan_fill_strat: nan_fill_constant}
                filename = f"./" + repo_name + "/" + dataset + "_" + adaptation_method + "_" + nan_fill_strat + "_" + \
                           algo + "_" + file_id
            elif nan_fill_strat == "knn":
                param_transport['nan_fill'] = {nan_fill_strat: n_neighbors}
                filename = f"./" + repo_name + "/" + dataset + "_" + adaptation_method + "_" + str(
                    n_neighbors) + "nn_" + algo + "_" + file_id
            else:
                param_transport['nan_fill'] = nan_fill_strat
                filename = f"./" + repo_name + "/" + dataset + "_" + adaptation_method + "_" + nan_fill_strat + "_" + \
                           algo + "_" + file_id

    apTrain, apTest, apClean, apTarget = train_model(X_source, y_source, X_target, y_target, X_clean, params_model,
                                                     normalizer, rescale, algo)
    if "OT" in adaptation_method:
        results = save_results(adaptation_method, dataset, algo, apTrain, apTest, apClean, apTarget, params_model,
                               param_transport, start, filename, results, param_transport_true_label)
    else:
        results = save_results(adaptation_method, dataset, algo, apTrain, apTest, apClean, apTarget, params_model,
                               param_transport, start, filename, results)


def toy_example(argv, adaptation="UOT", filename="", transpose=True, algo="XGBoost"):
    """

    :param argv:
    :param adaptation: type of adaptation wanted, default : "UOT",
                        possible values : "JCPOT", "SA", "OT", "UOT", "CORAL", "reweight_UOT"
    :param filename: name of the file where results are saved
    :param transpose: default : True (the targets are projected in the Source domain),
    False (the sources are projected in the Target domain)
    :param algo: algorithm to use for the learning
    :return:
    """
    seed = 1
    if len(argv) == 2:
        seed = int(argv[1])

    results = {}

    for dataset in ['abalone20']:  # , 'abalone17', 'satimage', 'abalone8']:  # ['abalone8']:  #

        start = time.time()
        now = datetime.now()
        file_id = now.strftime("%H%M%f")
        X, y = data_recovery(dataset)
        dataset_name = dataset
        pctPos = 100 * len(y[y == 1]) / len(y)
        dataset = "{:05.2f}%".format(pctPos) + " " + dataset
        results[adaptation] = {}
        print(dataset)
        np.random.seed(seed)
        random.seed(seed)

        normalizer = get_normalizer_data(X, "Outliers_Robust")
        X = set_nan_to_zero(X)
        X = normalizer.transform(X)

        # import the tuned parameters of the model for this dataset
        params_model = import_hyperparameters(dataset_name, "hyperparameters_toy_dataset.csv", toy_example=True)
        param_transport = dict()

        # Split the dataset between the source and the target(s)
        Xsource, Xtarget, ysource, ytarget = train_test_split(X, y, shuffle=True,
                                                              stratify=y,
                                                              random_state=1234,
                                                              test_size=0.51)
        # Keep a clean backup of Xtarget before degradation.
        Xclean = Xtarget.copy()
        # for loop -> degradation of the target
        # 3 features are deteriorated : the 2nd, the 3rd and the 4th
        for feat, coef in [(2, 0.1), (3, 10), (4, 0)]:
            # for features 2 and 3, their values are multiplied by a coefficient
            # resp. 0.1 and 10
            if coef != 0:
                Xtarget[:, feat] = Xtarget[:, feat] * coef
            # for feature 4, some of its values are (randomly) set to 0
            else:
                Xtarget[np.random.choice(len(Xtarget), int(len(Xtarget) / 2)), feat] = 0

        # Tune the hyperparameters of the adaptation by cross validation
        param_transport = adaptation_cross_validation(Xsource, ysource, Xtarget, params_model, normalizer,
                                                      transpose=transpose, adaptation=adaptation,
                                                      nb_training_iteration=2)
        # Domain adaptation
        Xsource, Xtarget, Xclean = adapt_domain(Xsource, ysource, Xtarget, Xclean, param_transport, transpose,
                                                adaptation)
        # Train and Learn model :

        # Save informations for the run :

        # Learning and saving parameters :
        # From the source, training and test set are created
        Xtrain, Xtest, ytrain, ytest = train_test_split(Xsource, ysource,
                                                        shuffle=True,
                                                        random_state=3456,
                                                        stratify=ysource,
                                                        test_size=0.3)

        Xtrain = normalizer.inverse_transform(Xtrain)
        Xtest = normalizer.inverse_transform(Xtest)
        Xtarget = normalizer.inverse_transform(Xtarget)

        apTrain, apTest, apClean, apTarget = applyAlgo(algo, params_model,
                                                       Xtrain, ytrain,
                                                       Xtest, ytest,
                                                       Xtarget, ytarget,
                                                       Xclean)

        results[adaptation][algo] = (apTrain, apTest, apClean, apTarget, params_model, param_transport,
                                     time.time() - start)
        print(dataset, algo, "Train AP {:5.2f}".format(apTrain),
              "Test AP {:5.2f}".format(apTest),
              "Clean AP {:5.2f}".format(apClean),
              "Target AP {:5.2f}".format(apTarget), params_model, param_transport,
              "in {:6.2f}s".format(time.time() - start))

        repo_id = now.strftime("%d%m")
        repo_name = "results" + repo_id
        if not os.path.exists(repo_name):
            try:
                os.makedirs(repo_name)
            except:
                pass
        if filename == "":
            filename = f"./" + repo_name + "/" + dataset_name + "_" + adaptation + "_" + algo + file_id
        f = gzip.open(filename, "wb")
        pickle.dump(results, f)
        f.close()


# in the main function, the thread are launched as follow :launch_thread(args).start()
def launch_thread(dataset, source_path, target_path, hyperparameter_file, filename="", algo="XGBoost",
                  adaptation_method="UOT", cv_with_true_labels=False, transpose=True, nb_iteration_cv=8,
                  select_feature=True, nan_fill_strat='mean', nan_fill_constant=0, n_neighbors=20, rescale=True):
    def handle():
        print("Thread is launch for dataset", dataset, "with algorithm", algo, "and adaptation", adaptation_method)

        launch_run(dataset, source_path, target_path, hyperparameter_file, filename, algo,
                   adaptation_method, cv_with_true_labels, transpose, nb_iteration_cv,
                   select_feature, nan_fill_strat, nan_fill_constant, n_neighbors, rescale)

    t = Thread(target=handle)
    return t


def start_evaluation(clust1: int, clust2: int, adaptation=None, rescale=False):
    for i in range(clust1, clust2):
        start_evaluation_cluster(i, adaptation, rescale)


def start_evaluation_cluster(i: int, adaptation=None, rescale=False):
    name = ["fraude1", "fraude2", "fraude3", "fraude4", "fraude5", "fraude6"]

    model_hyperparams = ["./hyperparameters/cluster20_fraude1_best_model_and_params.csv",
                         "./hyperparameters/cluster20_fraude2_best_model_and_params.csv",
                         "./hyperparameters/cluster20_fraude3_best_model_and_params.csv",
                         "./hyperparameters/cluster20_fraude4_best_model_and_params.csv",
                         "./hyperparameters/cluster20_fraude5_best_model_and_params.csv",
                         "./hyperparameters/cluster20_fraude6_best_model_and_params.csv"]

    source = ["./datasets/source_20_fraude1.csv",
              "./datasets/source_20_fraude2.csv",
              "./datasets/source_20_fraude3.csv",
              "./datasets/source_20_fraude4.csv",
              "./datasets/source_20_fraude5.csv",
              "./datasets/source_20_fraude6.csv"]

    target = ["./datasets/target_20_fraude1.csv",
              "./datasets/target_20_fraude2.csv",
              "./datasets/target_20_fraude3.csv",
              "./datasets/target_20_fraude4.csv",
              "./datasets/target_20_fraude5.csv",
              "./datasets/target_20_fraude6.csv"]

    if adaptation == None:
        adaptation_methods = ["UOT", "OT"]  # , "JCPOT", "reweight_UOT", "TCA", "SA", "NA", "CORAL"
    else:
        if type(adaptation) == str:
            adaptation_methods = [adaptation]
        else:
            adaptation_methods = adaptation

    for adaptation_method in adaptation_methods:
        if not rescale:
            launch_run(name[i], source[i], target[i], model_hyperparams[i], adaptation_method=adaptation_method,
                       nb_iteration_cv=4,
                       rescale=False)
        else:
            launch_run(name[i], source[i], target[i], model_hyperparams[i], adaptation_method=adaptation_method,
                       nb_iteration_cv=4,
                       rescale=True)


if __name__ == '__main__':
    # configure debugging tool
    ic.configureOutput(includeContext=True)

    if len(argv) > 1:
        if argv[1] == "-launch":
            if argv[4] == "False":
                rescale = False
            else:
                rescale = True

            start_evaluation_cluster(int(argv[2]), argv[3], rescale)
