# (setq python-shell-interpreter "./venv/bin/python")


# import tensorflow as tf
import numpy as np
# import imageio
import matplotlib.pyplot as plt
from numpy import linalg as LA
import time
from tqdm import trange
from sklearn.metrics import roc_curve
from scipy.spatial import ConvexHull
from sklearn import metrics
from sklearn.metrics import precision_recall_curve
from sklearn.metrics import accuracy_score
from sklearn.metrics import confusion_matrix
import scipy.sparse as sp
from sklearn.decomposition import SparseCoder
from sklearn.linear_model import LogisticRegression
from scipy.linalg import block_diag






DEBUG = False


class SDL_BCD():
    # Supervised Dictionary Learning by Block Coordinate Descent
    # Author: Joowon Lee and Hanbaek Lyu
    # Supservise NMF feature extraction via a classification task
    # NMF + Logistic Regression
    # Model: Data (X) \approx Dictionary (W) @ Code (H)
    #        Label (Y) \approx F(W.T @ X)
    # F = logit if Logistic Regression classifier, linear map if Linear Regression
    # In general, F could be any function that maps the reduced feature W.T @ X into predictive probabilities
    # e.g., F = Feedforward Neural Network
    # It is critical that we use W.T @ X instead of H for the reduced feature for classification tasks
    # For matrix completion, H can be learned from imcomplete observation and can be used to infer missing values
    # e.g., Convolutional Matrix Factorization by Donghyun Kim et al. (2016)

    # optimization framework:
    # (\hat{\W}, \hat{\Beta})
    #  argmin_{W,\beta} ( inf_H xi * | X - WH |^2  + ( | Y - F(W.T @ X, \beta) | ) + Regularizer
    # X: data matrix of size (d1 x n), Y: label matrix of size (d2 x n) \beta: linear coefficients of size (d2 x r)
    # r = n_components (int): number of columns in dictionary matrix W where each column represents on topic/feature
    # iter (int): number of iterations where each iteration is a call to step(...)

    def __init__(self,
                 X,  # [X = [X,Y] : data (d1 x n), label (d2 x n)]
                 X_auxiliary=None, # auxiliary data (d3 x n) that is subject to LR but not to NMF
                 X_test=None, # [X_test = [X_test, Y_test]] test set
                 X_test_aux=None, # aux test data (d3 x n)
                 n_components=100,  # =: r = number of columns in dictionary matrices W, W'
                 iterations=500,
                 ini_loading=None,  # Initialization for [W1, W2, W3] = [(dict), (reg. coeff), (reg. coeff for aux var.)]
                 #W1.shape = [d1, r], W2.shape = [d2, r], W3.shape = [d3, r]
                 ini_code=None,
                 xi = None, # weight for dim reduction vs. prediction trade-off
                 L1_reg=[0,0,0], # L1 regularizer for code H, dictioanry W[0], and regression params W[1]
                 L2_reg=[0,0,0], # L2 regularizer for code H, dictioanry W[0], and regression params W[1]
                 nonnegativity=[True,True,False], # nonnegativity constraints on code H, dictionary W[0], reg params W[1]
                 full_dim=False): # if true, dictionary matrix W[0] is Id with size d1 x d1 -- no dimension reduction

        self.X = X
        self.X_auxiliary = X_auxiliary
        self.d3 = 0 # auxiliary data dim
        self.nonnegativity = nonnegativity
        if X_auxiliary is not None:
            self.d3 = X_auxiliary.shape[0]

        self.X_test = X_test
        self.X_test_aux = X_test_aux
        self.n_components = n_components
        self.iterations = iterations
        self.ini_code = ini_code
        if ini_code is None:
            self.ini_code = np.random.rand(n_components, X[0].shape[1])

        self.loading = ini_loading
        if ini_loading is None:
            d1, n = X[0].shape
            d2, n = X[1].shape
            r = n_components
            self.loading = [np.random.rand(X[0].shape[0], r), 1-2*np.random.rand(X[1].shape[0], r + 1 + self.d3)]  # additional first column for constant terms in Logistic Regression
            # add additional d3 columns of regression coefficients for the auxiliary variables
        print('initial loading beta', self.loading[1])

        self.xi = xi
        self.L1_reg = L1_reg
        self.L2_reg = L2_reg
        self.code = np.zeros(shape=(n_components, X[0].shape[1]))
        self.full_dim = full_dim
        self.result_dict = {}
        self.result_dict.update({'xi' : self.xi})
        self.result_dict.update({'L1_reg' : self.L1_reg})
        self.result_dict.update({'L2_reg' : self.L2_reg})
        self.result_dict.update({'nonnegativity' : self.nonnegativity})
        self.result_dict.update({'n_components' : self.n_components})


    def sparse_code(self, X, W, sparsity=0):
        # Same function as OMF

        '''
        Given data matrix X and dictionary matrix W, find
        code matrix H such that W*H approximates X

        args:
            X (numpy array): data matrix with dimensions: features (d) x samples (n)
            W (numpy array): dictionary matrix with dimensions: features (d) x topics (r)

        returns:
            H (numpy array): code matrix with dimensions: topics (r) x samples(n)
        '''



        if DEBUG:
            print('sparse_code')
            print('X.shape:', X.shape)
            print('W.shape:', W.shape, '\n')

        # initialize the SparseCoder with W as its dictionary
        # then find H such that X \approx W*H
        coder = SparseCoder(dictionary=W.T, transform_n_nonzero_coefs=None,
                            transform_alpha=sparsity, transform_algorithm='lasso_lars', positive_code=True)
        # alpha = L1 regularization parameter.
        H = coder.transform(X.T)

        # transpose H before returning to undo the preceding transpose on X
        # print('!!! sparse_code: Start')
        return H.T


    def update_beta_logistic(self, Y, W0, input, r, a1=0, sub_iter=2, stopping_diff=0.1, nonnegativity=True, history=1):
        '''
        Y = (p' x n), W = (p' x (r+1)), H = (r' x n), H' = np.vstack((np.ones(n), H))
        W0 = [W_beta  W_beta_aux]
        H = [H               ]
            [self.X_auxiliary]
        Logistic Regression: Y ~ Bernoulli(P), logit(P) = W H'
        MLE -->
        Find \hat{W} = argmin_W ( sum_j ( log(1+exp(W H_j) ) - Y (W H).T ) ) within radius r from W0
        Use row-wise projected gradient descent
        '''

        d1 = self.X[0].shape[0] # data dim
        H = np.vstack((np.ones(Y.shape[1]), input))
        #H -= np.mean(H)
        #H /= np.std(H)

        if self.d3 > 0:
            H = np.vstack((H, self.X_auxiliary)) # add additional rows for the auxiliary explanatory variables

        A = H @ H.T
        P = 1/(1+np.exp(-W0 @ H))  # probability matrix, same shape as Y

        W1 = W0.copy()
        i = history
        dist = 1
        while (i < sub_iter) and (dist > stopping_diff):
            # W1_old = W1.copy()
            for k in np.arange(W0.shape[0]):
                grad = H @ (P[k,:] - Y[k,:]).T + a1 * np.ones(W0.shape[1])
                # H1[k, :] = H1[k,:] - (1 / (A[k, k] + np.linalg.norm(grad, 2))) * grad
                W1[k, :] = W1[k, :] - (1 / (((i + 10) ** (0.5)) * (A[k, k] + 1))) * grad

                if nonnegativity:
                    W1[k, :] = np.maximum(W1[k, :], np.zeros(shape=(W1.shape[1],)))  # nonnegativity constraint

                if r is not None:  # usual sparse coding without radius restriction
                    d = np.linalg.norm(W1 - W0, 2)
                    W1 = W0 + (r / max(r, d)) * (W1 - W0)

                W0 = W1

            dist = np.linalg.norm(W1 - W0, 2) / np.linalg.norm(W0, 2)
            #print('!!! dist', dist)
            i = i + 1
            # print('!!!! i', i)  # mostly the loop finishes at i=1 except the first round
        return W1

    def update_dict_joint_logistic(self, X, H, W0, r, a1=0, a2=0, sub_iter=2, stopping_diff=0.1, nonnegativity=True, subsample_size=None):
        '''
        X = [X0, X1]
        W = [W0, W1+W2]
        Find \hat{W} = argmin_W ( || X0 - W0 H||^2 + alpha|H| + Logistic_Loss(W[0].T @ X1, W[1])) within radius r from W0
        Compressed data = W[0].T @ X0 instead of H
        '''

        if W0 is None:
            W0 = np.random.rand(X[0].shape[0], self.n_components)
            print('!!! W0.shape', W0.shape)

        #if not self.full_dim:
        A = H @ H.T

        W1 = W0[0].copy()
        i = 0
        dist = 1
        idx = np.arange(X[0].shape[0])
        while (i < sub_iter) and (dist > stopping_diff):
            W1_old = W1.copy()

            X0_comp = W1.T @ X[0]
            H1_ext = np.vstack((np.ones(X[1].shape[1]), X0_comp))
            if self.X_auxiliary is not None:
                H1_ext = np.vstack((H1_ext, self.X_auxiliary[:,:]))
                # add additional rows for the auxiliary explanatory variables

            # P = probability matrix, same shape as X1
            D = W0[1] @ H1_ext
            P = 1 / (1 + np.exp(-D))

            if not self.full_dim:
                grad_MF = (W1 @ H - X[0]) @ H.T
                grad_pred = X[0] @ (P-X[1]).T @ W0[1][:, 1:self.n_components+1] # exclude the first column of W[1] (intercept terms)
                grad = self.xi * grad_MF + grad_pred + a1 * np.sign(W1)*np.ones(shape=W1.shape) + a2 * W1
                # grad = grad_MF

                W1 -= (1 / (((i + 10) ** (0.5)) * (np.trace(A) + 1))) * grad

            if r is not None:  # usual sparse coding without radius restriction
                d = np.linalg.norm(W1 - W0[0], 2)
                W1 = W0[0] + (r / max(r, d)) * (W1 - W0[0])
            W0[0] = W1

            if nonnegativity:
                W1 = np.maximum(W1, np.zeros(shape=W1.shape))  # nonnegativity constraint

            dist = np.linalg.norm(W1 - W1_old, 2) / np.linalg.norm(W1_old, 2)
            dist = 1
            # print('!!! dist', dist)
            # H1_old = H1
            i = i + 1
            # print('!!!! i', i)  # mostly the loop finishes at i=1 except the first round


        return W1

    def update_code_joint_logistic(self, X, W, H0, r,
                                   a1=0, a2=0, sub_iter=2,
                                   stopping_diff=0.1, nonnegativity=True,
                                   xi = 0,
                                   subsample_size=None):
        '''
        X = [X0, X1]
        W = [W0, W1+W2]
        Find \hat{H} = argmin_H ( xi * || X0 - W0 H||^2 + alpha|H| + Logistic_Loss(X1, [W1|W2], H)) within radius r from H0
        Use row-wise projected gradient descent
        '''

        if H0 is None:
            H0 = np.random.rand(W[0].shape[1], X[0].shape[1])
            # print('!!! H0.shape', H0.shape)

        if not self.full_dim:
            A = W[0].T @ W[0]
            B = W[0].T @ X[0]

        H1 = H0.copy()
        i = 0
        dist = 1
        idx = np.arange(X[0].shape[1])
        while (i < sub_iter) and (dist > stopping_diff):
            H1_old = H1.copy()
            for k in np.arange(H1.shape[0]):
                if subsample_size is not None:
                    idx = np.random.randint(X[0].shape[1], size=subsample_size)

                H1_ext = np.vstack((np.ones(len(idx)), H1[:,idx]))
                if self.X_auxiliary is not None:
                    H1_ext = np.vstack((H1_ext, self.X_auxiliary[:,idx]))
                    # add additional rows for the auxiliary explanatory variables

                # P = probability matrix, same shape as X1
                D = W[1] @ H1_ext
                P = 1 / (1 + np.exp(-D))

                if self.full_dim:
                    grad = np.diag(W[1][:,k]) @ (P-X[1][:,idx])
                    H1[k, idx] = H1[k, idx] - (1 / (((i + 10) ** (0.5)) * (0 + 1))) * grad
                else:
                    grad_MF = (np.dot(A[k, :], H1[:,idx]) - B[k, idx])
                    #print('W[1].shape', W[1].shape)
                    grad_pred = np.diag(W[1][:,k+1]) @ (P-X[1][:,idx])
                    grad =  xi * grad_MF + grad_pred + a1 * np.sign(H1[k,idx])*np.ones(len(idx)) + a2 * H1[k, idx]
                    H1[k, idx] = H1[k, idx] - (1 / (((i + 10) ** (0.5)) * (A[k, k] + 1))) * grad

                if nonnegativity:
                    H1[k, idx] = np.maximum(H1[k, idx], np.zeros(shape=(len(idx),)))  # nonnegativity constraint

                if r is not None:  # usual sparse coding without radius restriction
                    d = np.linalg.norm(H1 - H0, 2)
                    H1 = H0 + (r / max(r, d)) * (H1 - H0)
                H0 = H1

            dist = np.linalg.norm(H1 - H1_old, 2) / np.linalg.norm(H1_old, 2)
            # print('!!! dist', dist)
            # H1_old = H1
            i = i + 1
            # print('!!!! i', i)  # mostly the loop finishes at i=1 except the first round


        return H1


    def fit(self,
            option = "filter", #or "feature"
            iter=100,
            beta=1,
            dict_update_freq=1,
            subsample_size=None,
            subsample_ratio_code=None,
            search_radius_const=1000,
            if_compute_recons_error=False,
            update_nuance_param=False,
            if_validate=False):
        '''
        Given input X = [data, label] and initial loading dictionary W_ini, find W = [dict, beta] and code H
        by two-block coordinate descent: [dict, beta] --> H, H--> [dict, beta]
        Use Supervised NMF model
        option = 'filter' : filter-based SDL
        option = 'feature' : feature-based SDL
        update_nuance_param = True means self.xi is updated by the MLE (sample variance) each iteration
        '''
        X = self.X
        r = self.n_components
        n = X[0].shape[1]

        #H = np.random.rand(r, n)
        #W = [np.random.rand(X[0].shape[0], r), np.random.rand(X[1].shape[0], r + 1 + self.d3)]  # additional first column for constant terms in Logistic Regression
        H = self.ini_code
        W = self.loading

        prediction_method_list = ['filter']
        if option == 'feature':
            prediction_method_list = ['naive']

        if self.full_dim:
            r = X[0].shape[0]
            W = [1, np.random.rand(X[1].shape[0], r + 1 + self.d3)] # don't use np.identity(r) for efficient computation
            H = X[0]

        time_error = np.zeros(shape=[0, 3])
        elapsed_time = 0
        total_error = 0

        for step in trange(int(iter)):
            start = time.time()
            if beta is not None:
                search_radius = search_radius_const * (float(step + 1)) ** (-beta) / np.log(float(step + 2))
            else:
                search_radius = None

            if self.full_dim:
                W[1] = self.update_beta_logistic(X[1], W[1], H, sub_iter = step+1,
                                                 r=None, history=step)
            elif option == "filter":
                # Dictionary Update
                if step % dict_update_freq == 0:
                    W[0] = self.update_dict_joint_logistic(X, H, W, stopping_diff=0.0001,
                                                     sub_iter = 5,
                                                     r=search_radius, nonnegativity=self.nonnegativity[1],
                                                     a1=self.L1_reg[1], a2=self.L2_reg[1],
                                                     subsample_size = None)

                    W[0] /= np.linalg.norm(W[0])


                # Code Update
                H = update_code_within_radius(X[0], W[0], H, r=search_radius,
                                            a1=self.L1_reg[0], a2=self.L2_reg[0],
                                            nonnegativity=self.nonnegativity[0])


                # Regression Parameters Update
                X0_comp = W[0].T @ X[0]
                if self.X_auxiliary is not None:
                    X0_comp = np.vstack((X0_comp, self.X_auxiliary[:,:]))
                clf = LogisticRegression(random_state=0).fit(X0_comp.T, self.X[1][0,:])
                W[1][0,1:] = clf.coef_[0]
                W[1][0,0] = clf.intercept_[0]

            elif option == "feature":
                if (step % dict_update_freq == 0):

                    W[0] = update_code_within_radius(X[0].T, H.T, W[0].T, stopping_grad_ratio=0.01,
                                                     r=search_radius, nonnegativity=self.nonnegativity[1],
                                                     a1=self.L1_reg[1], a2=self.L2_reg[1]).T

                    W[0] /= np.linalg.norm(W[0])


                # Beta
                H1 = H.copy()
                if self.X_auxiliary is not None:
                    H1 = np.vstack((H, self.X_auxiliary[:,:]))
                clf = LogisticRegression(random_state=0).fit(H1.T, self.X[1][0,:])
                W[1][0,1:] = clf.coef_[0]
                W[1][0,0] = clf.intercept_[0]

                # H
                H = self.update_code_joint_logistic(X, W, H, r=search_radius,
                                                    a1=self.L1_reg[0], a2=self.L2_reg[0],
                                                    xi = self.xi,
                                                    sub_iter=2,
                                                    stopping_diff=0.0001,
                                                    nonnegativity=self.nonnegativity[0],
                                                    subsample_size=int(X[0].shape[1]//10))

            if update_nuance_param:
                self.xi = (1/(2*r*n)) * np.linalg.norm((X[0] - W[0] @ H).reshape(-1, 1), ord=2)**2
                print('xi updated by MLE:', self.xi)

            end = time.time()
            elapsed_time += end - start

            self.result_dict.update({'loading': W})
            self.result_dict.update({'code': H})
            self.result_dict.update({'iter': iter})
            self.result_dict.update({'n_components': self.n_components})
            self.result_dict.update({'dict_update_freq' : dict_update_freq})

            self.loading = W
            self.code = H



            if (step % 10) == 0:
                if if_compute_recons_error:
                    # print the error every 50 iterations
                    if self.full_dim:
                        error_data = np.linalg.norm((X[0] - H).reshape(-1, 1), ord=2)**2
                    else:
                        error_data = np.linalg.norm((X[0] - W[0] @ H).reshape(-1, 1), ord=2)**2
                    rel_error_data = error_data / np.linalg.norm(X[0].reshape(-1, 1), ord=2)**2

                    X0_comp = W[0].T @ X[0]
                    X0_ext = np.vstack((np.ones(X[1].shape[1]), X0_comp))
                    if self.d3>0:
                        X0_ext = np.vstack((X0_ext, self.X_auxiliary))
                    P_pred = np.matmul(W[1], X0_ext)
                    P_pred = 1 / (np.exp(-P_pred) + 1)
                    # print('!!! error norm', np.linalg.norm(X[1][0, :]-P_pred[0,:])/X[1].shape[1])
                    fpr, tpr, thresholds = metrics.roc_curve(X[1][0, :], P_pred[0,:], pos_label=None)
                    mythre = thresholds[np.argmax(tpr - fpr)]
                    myauc = metrics.auc(fpr, tpr)
                    self.result_dict.update({'Training_threshold':mythre})
                    self.result_dict.update({'Training_AUC':myauc})
                    print('--- Training --- [threshold, AUC] = ', [np.round(mythre,3), np.round(myauc,3)])
                    error_label = np.sum(np.log(1+np.exp(W[1] @ X0_ext))) - X[1] @ (W[1] @ X0_ext).T
                    error_label = error_label[0][0]

                    total_error_new = error_label + self.xi * error_data

                    time_error = np.append(time_error, np.array([[elapsed_time, error_data, error_label]]), axis=0)
                    print('--- Iteration %i: Training loss --- [Data, Label, Total] = [%f.3, %f.3, %f.3]' % (step, error_data, error_label, total_error_new))

                    self.result_dict.update({'Relative_reconstruction_loss (training)': rel_error_data})
                    self.result_dict.update({'Classification_loss (training)': error_label})
                    self.result_dict.update({'time_error': time_error.T})

                    # stopping criterion
                    if (total_error > 0) and (total_error_new > 1.001 * total_error):
                        print("Early stopping: training loss increased")
                        self.result_dict.update({'iter': step})
                        break
                    else:
                        total_error = total_error_new


                if if_validate and (step>1):
                    self.validation(result_dict = self.result_dict,
                                    prediction_method_list=prediction_method_list,
                                    verbose=True)
                    threshold = self.result_dict.get('Opt_threshold')
                    ACC = self.result_dict.get('Accuracy')
                    if ACC>0.99:
                        # terminate the training as soon as AUC>0.9 in order to avoid overfitting
                        print('!!! --- Validation (Stopped) --- [threshold, ACC] = ', [np.round(threshold,3), np.round(ACC,3)])
                        break

        ### fine-tune beta
        clf = LogisticRegression(random_state=0).fit(X0_comp.T, self.X[1][0,:])
        W[1][0,1:] = clf.coef_[0]

        self.validation(result_dict = self.result_dict, prediction_method_list=prediction_method_list)
        #threshold = self.result_dict.get('Opt_threshold')
        #AUC = self.result_dict.get('AUC')
        #print('!!! FINAL [threshold, AUC] = ', [np.round(threshold,3), np.round(AUC,3)])

        return self.result_dict

    def validation(self,
                    result_dict=None,
                    X_test = None,
                    X_test_aux = None,
                    sub_iter=100,
                    verbose=False,
                    stopping_grad_ratio=0.0001,
                    prediction_method_list = ['filter', 'naive', 'alt', 'exhaustive']):
        '''
        Given input X = [data, label] and initial loading dictionary W_ini, find W = [dict, beta] and code H
        by two-block coordinate descent: [dict, beta] --> H, H--> [dict, beta]
        Use Logistic MF model
        '''
        if result_dict is None:
            result_dict = self.result_dict
        if X_test is None:
            X_test = self.X_test
        if X_test_aux is None:
            X_test_aux = self.X_test_aux

        test_X = X_test[0]
        test_Y = X_test[1]

        W = result_dict.get('loading')
        beta = W[1].T
        # pred_threshold = result_dict.get('Opt_threshold (training)')
        # prediction threshold learned from training data


        for pred_type in prediction_method_list:
            print('!!! pred_type', pred_type)

            P_pred, H_test, Y_pred = self.predict(X_test = test_X,
                                            X_test_aux=X_test_aux,
                                            W=W,
                                            pred_threshold = None,
                                            method=pred_type) #or 'exhaust' or # naive

            fpr, tpr, thresholds = metrics.roc_curve(test_Y[0, :], P_pred, pos_label=None)
            mythre_test = thresholds[np.argmax(tpr - fpr)]
            myauc_test = metrics.auc(fpr, tpr)


            mcm = confusion_matrix(test_Y[0,:], Y_pred)
            tn = mcm[0, 0]
            tp = mcm[1, 1]
            fn = mcm[1, 0]
            fp = mcm[0, 1]

            accuracy = (tp + tn) / (tp + tn + fp + fn)
            misclassification = 1 - accuracy
            sensitivity = tp / (tp + fn)
            specificity = tn / (tn + fp)
            precision = tp / (tp + fp)
            recall = tp / (tp + fn)
            fall_out = fp / (fp + tn)
            miss_rate = fn / (fn + tp)
            F_score = 2 * precision * recall / ( precision + recall )

            # Compute test data reconstruction loss
            H_test = self.sparse_code(X_test[0], W[0])
            error_data = np.linalg.norm((X_test[0] - W[0] @ H_test).reshape(-1, 1), ord=2)
            rel_error_data = error_data / np.linalg.norm(X_test[0].reshape(-1, 1), ord=2)


            # Save results
            result_dict.update({'Relative_reconstruction_loss (test)': rel_error_data})
            result_dict.update({'Y_test': test_Y})
            result_dict.update({'P_pred': P_pred})
            result_dict.update({'Y_pred': Y_pred})
            result_dict.update({'AUC': myauc_test})
            result_dict.update({'Opt_threshold': mythre_test})
            result_dict.update({'Accuracy': accuracy})
            result_dict.update({'Misclassification': misclassification})
            result_dict.update({'Precision': precision})
            result_dict.update({'Recall': recall})
            result_dict.update({'Sensitivity': sensitivity})
            result_dict.update({'Specificity': specificity})
            result_dict.update({'F_score': F_score})
            result_dict.update({'Fall_out': fall_out})
            result_dict.update({'Miss_rate': miss_rate})

            if verbose:
                fpr, tpr, thresholds = metrics.roc_curve(test_Y[0, :], P_pred, pos_label=None)
                mythre = thresholds[np.argmax(tpr - fpr)] # optimal prediction threshold for validation
                myauc = metrics.auc(fpr, tpr)
                # print('--- Validation --- [threshold, AUC, accuracy] = ', [np.round(mythre,3), np.round(myauc,3), np.round(accuracy, 3)])
                print('--- Validation --- [threshold, AUC, Accuracy, F score] = ', [np.round(mythre,3), np.round(myauc,3), np.round(accuracy, 3), np.round(F_score,3)])

        return result_dict



    def predict(self,
                X_test,
                X_test_aux=None,
                W=None,
                iter=10,
                pred_threshold=None,
                search_radius_const=10,
                method='naive' #or 'exhaustive' or 'naive' or 'alt' or 'filter'
                ):
        '''
        Given input X = [data, ??] and loading dictionary W = [dict, beta], find missing label Y and code H
        by two-block coordinate descent
        '''

        r = self.n_components
        n = X_test.shape[1]
        if W is None:
            W = self.loading
        # print("-- W[0][0,0]", np.linalg.norm(W[0][0,0]))

        if pred_threshold is None:
            if method == 'filter':
                # Get threshold from training set
                X0_comp = W[0].T @ self.X[0]
                X0_ext = np.vstack((np.ones(self.X[1].shape[1]), X0_comp))
                if self.d3>0:
                    X0_ext = np.vstack((X0_ext, self.X_auxiliary))
                P_pred = np.matmul(W[1], X0_ext)
                P_pred = 1 / (np.exp(-P_pred) + 1)
                # print('!!! error norm', np.linalg.norm(X[1][0, :]-P_pred[0,:])/X[1].shape[1])
                fpr, tpr, thresholds = metrics.roc_curve(self.X[1][0, :], P_pred[0,:], pos_label=None)
                pred_threshold = thresholds[np.argmax(tpr - fpr)]
                myauc_training = metrics.auc(fpr, tpr)

            else:
                # Get threshold from training set
                X0_comp = self.sparse_code(self.X[0], W[0])
                X0_ext = np.vstack((np.ones(self.X[1].shape[1]), X0_comp))
                if self.d3>0:
                    X0_ext = np.vstack((X0_ext, self.X_auxiliary))
                P_pred = np.matmul(W[1], X0_ext)
                P_pred = 1 / (np.exp(-P_pred) + 1)
                # print('!!! error norm', np.linalg.norm(X[1][0, :]-P_pred[0,:])/X[1].shape[1])
                fpr, tpr, thresholds = metrics.roc_curve(self.X[1][0, :], P_pred[0,:], pos_label=None)
                pred_threshold = thresholds[np.argmax(tpr - fpr)]
                myauc_training = metrics.auc(fpr, tpr)

            self.result_dict.update({'Training_threshold': pred_threshold})


        ### Make prediction

        if method == 'filter':
            # Compute accuracy metrics for the test set
            H = W[0].T @ X_test
            if X_test_aux is not None:
                H = np.vstack((H, X_test_aux))
            H2 = np.vstack((np.ones(H.shape[1]), H))
            P_pred = np.matmul(H2.T, W[1].T)
            P_pred = 1 / (np.exp(-P_pred) + 1)  # predicted probability for Y_test

            Y_hat = P_pred.copy()
            Y_hat[Y_hat < pred_threshold] = 0
            Y_hat[Y_hat >= pred_threshold] = 1

            #print('P_pred.shape', P_pred.shape)
            #print('Y_hat.shape', Y_hat.shape)

        elif method == 'alt':
            for step in range(int(iter)):
                start = time.time()
                # search_radius = search_radius_const * (float(step + 1)) ** (-beta) / np.log(float(step + 2))

                # Update the missing label P_pred
                H_ext = np.vstack((np.ones(X_test.shape[1]), H))
                P_pred = np.matmul(W[1], H_ext)
                P_pred = 1 / (np.exp(-P_pred) + 1)
                X = [X_test, P_pred]

                # Update code
                Y_hat = P_pred.copy()
                Y_hat[Y_hat < pred_threshold] = 0
                Y_hat[Y_hat >= pred_threshold] = 1
                P_pred = P_pred[0,:]
                Y_hat = Y_hat[0,:]


        elif method == 'naive':
            #print('naive prection..')

            # Prediction for test set
            H = self.sparse_code(X_test, W[0])
            #print('---- H naive shape', H.shape)
            H_ext = np.vstack((np.ones(X_test.shape[1]), H))
            if X_test_aux is not None:
                H_ext = np.vstack((H_ext, X_test_aux))
            P_pred = np.matmul(W[1], H_ext)
            P_pred = 1 / (np.exp(-P_pred) + 1)

            # threshold predictive probabilities to get predictions
            Y_hat = P_pred.copy()
            Y_hat[Y_hat < pred_threshold] = 0
            Y_hat[Y_hat >= pred_threshold] = 1

            P_pred = P_pred[0,:]
            Y_hat = Y_hat[0,:]

        elif method == 'alt':
            #print('alternating prection..')
            H = np.random.rand(r,n)
            Y_hat = np.random.rand(self.X[1].shape[0], X_test.shape[1])
            for step in trange(int(200)):
                X = [X_test, Y_hat]

                # Update code
                radius = 10/(step+1)
                H = self.update_code_joint_logistic(X, W, H, r=radius, sub_iter = 2, stopping_diff=0.0001)
                # Update the missing label P_pred
                H_ext = np.vstack((np.ones(X_test.shape[1]), H))

                if X_test_aux is not None:
                    H_ext = np.vstack((H_ext, X_test_aux))

                P_pred = np.matmul(W[1], H_ext)
                P_pred = 1 / (np.exp(-P_pred) + 1)
                Y_hat = P_pred

                # threshold predictive probabilities to get predictions
            P_pred = P_pred[0,:]
            Y_hat[Y_hat < pred_threshold] = 0
            Y_hat[Y_hat >= pred_threshold] = 1
            Y_hat = Y_hat[0,:]

        elif method == 'exhaustive':
            print('exhaustive prection..')
            # Run over all possible y_hat values, do supervised sparse coding,
            # and find the one that gives minimum loss
            H = []
            Y_hat = []
            for i in trange(n):
                loss_list = []
                h_list = []
                x_test = X_test[:,i][:,np.newaxis]

                for j in np.arange(2):
                    y_guess = np.asarray([[j]])
                    x_guess = [x_test, y_guess]
                    h = self.update_code_joint_logistic(x_guess, W, xi=self.xi, sub_iter=40,
                                                        stopping_diff=0.001, H0=None, r=None)
                    h_ext = np.vstack((np.ones(1), h))
                    error_data = np.linalg.norm((x_test - W[0] @ h).reshape(-1, 1), ord=2) ** 2
                    error_label = np.sum(np.log(1+np.exp(W[1] @ h_ext))) - y_guess @ (W[1] @ h_ext).T
                    loss = (error_label + self.xi * error_data)[0,0]
                    # print('[j, loss] = ', [j, loss])
                    loss_list.append(loss)
                    h_list.append(h)

                idx = np.argsort(loss_list)
                #print('loss_list', loss_list)
                # print('idx', idx)
                y_hat = idx[0]
                h_hat = h_list[idx[0]][:,0]

                Y_hat.append(y_hat)
                H.append(h_hat)

            Y_hat = np.asarray(Y_hat)
            #print('--- Y_hat', Y_hat)
            H = np.asarray(H).T
            H -= np.mean(H)
            H_ext = np.vstack((np.ones(X_test.shape[1]), H))
            P_pred = np.matmul(W[1], H_ext)
            P_pred = 1 / (np.exp(-P_pred) + 1)
            P_pred = P_pred[0,:]

        self.result_dict.update({'code_test': H})
        self.result_dict.update({'P_pred': P_pred})
        self.result_dict.update({'Y_hat': Y_hat})

        return P_pred, H, Y_hat

###### Helper functions

def sparseness(x):
    """Hoyer's measure of sparsity for a vector"""
    sqrt_n = np.sqrt(len(x))
    return (sqrt_n - np.linalg.norm(x, 1) / norm(x)) / (sqrt_n - 1)


def safe_vstack(Xs):
    if any(sp.issparse(X) for X in Xs):
        return sp.vstack(Xs)
    else:
        return np.vstack(Xs)


def update_code_within_radius(X, W, H0, r, a1=0, a2=0,
                              sub_iter=[2], stopping_grad_ratio=0.0001,
                              subsample_ratio=None, nonnegativity=True,
                              use_line_search=False):
    '''
    Find \hat{H} = argmin_H ( | X - WH| + alpha|H| ) within radius r from H0
    Use row-wise projected gradient descent
    Do NOT sparsecode the whole thing and then project -- instable
    12/5/2020 Lyu

    For NTF problems, X is usually tall and thin so it is better to subsample from rows
    12/25/2020 Lyu

    Apply single round of AdaGrad for rows, stop when gradient norm is small and do not make update
    12/27/2020 Lyu
    '''

    # print('!!!! X.shape', X.shape)
    # print('!!!! W.shape', W.shape)
    # print('!!!! H0.shape', H0.shape)

    if H0 is None:
        H0 = np.random.rand(W.shape[1], X.shape[1])
    H1 = H0.copy()
    i = 0
    dist = 1
    idx = np.arange(X.shape[0])
    H1_old = H1.copy()

    A = W.T @ W
    B = W.T @ X

    while (i < np.random.choice(sub_iter)):
        if_continue = np.ones(H0.shape[0])  # indexed by rows of H

        for k in [k for k in np.arange(H0.shape[0]) if if_continue[k]>0.5]:

            grad = np.dot(A[k, :], H1) - B[k, :]
            grad += a1 * np.sign(H1[k, :]) * np.ones(H0.shape[1]) + a2 * H1[k, :]
            grad_norm = np.linalg.norm(grad, 2)

            # Initial step size
            step_size = 1/(A[k,k]+1)
            # step_size = 1 / (np.trace(A)) # use the whole trace
            # step_size = 1
            if r is not None:  # usual sparse coding without radius restriction
                d = step_size * grad_norm
                step_size = (r / max(r, d)) * step_size

            H1_temp = H1.copy()
            # loss_old = np.linalg.norm(X - W @ H1)**2
            H1_temp[k, :] = H1[k, :] - step_size * grad
            if nonnegativity:
                H1_temp[k,:] = np.maximum(H1_temp[k,:], np.zeros(shape=(H1.shape[1],)))  # nonnegativity constraint
            #loss_new = np.linalg.norm(X - W @ H1_temp)**2
            #if loss_old > loss_new:

                # print('recons_loss:' , np.linalg.norm(X - W @ H1, ord=2) / np.linalg.norm(X, ord=2))

            """
            if use_line_search:
            # Armijo backtraking line search
                m = grad.T @ H1[k,:]
                H1_temp = H1.copy()
                loss_old = np.linalg.norm(X - W @ H1)**2
                loss_new = 0
                count = 0
                while (count==0) or (loss_old - loss_new < 0.1 * step_size * m):
                    step_size /= 2
                    H1_temp[k, :] = H1[k, :] - step_size * grad
                    if nonnegativity:
                        H1_temp[k,:] = np.maximum(H1_temp[k,:], np.zeros(shape=(H1.shape[1],)))  # nonnegativity constraint
                    loss_new = np.linalg.norm(X - W @ H1_temp)**2
                    count += 1
            """
            H1 = H1_temp

        i = i + 1


    return H1


def block_dict_column_update(X, H, W0=None, r=None, alpha=0):
    '''
    Use column-wise block minimization for dictionary upate to induce L1 sparsity on each columns
    '''
    if W0 is None:
        W0 = np.random.rand(self.X[0].shape[0], self.n_components)

    W1 = W0.copy()

    for k in np.arange(self.n_components):
        W1[:,k] = update_code_within_radius(X[0].T, H.T, W[0].T, r=search_radius, sparsity=self.a0).T



    while (i < np.random.choice(sub_iter)):
        if_continue = np.ones(W0.shape[1])  # indexed by columns of W
        W1_old = W1.copy()

        A = W[:,:].T @ W[:,:]
        B = W[:,:].T @ X[:,:]

        for k in [k for k in np.arange(H0.shape[0]) if if_continue[k]>0.5]:
            # row-wise gradient descent
            n = H0.shape[1]
            # grad_sparseness = (1/(np.sqrt(n)-1)) * (np.ones(H0.shape[1])-2*np.linalg.norm(H0[k,:],1)*(np.linalg.norm(H0[k,:],1)**(-2/3))*H0[k,:])/np.linalg.norm(H0[k,:],2)
            grad = (np.dot(A[k, :], H1) - B[k, :] + sparsity * np.ones(H0.shape[1]))
            grad_norm = np.linalg.norm(grad, 2)
            step_size = (1 / (((i + 2) ** (1)) * (A[k, k] + 1)))
            if r is not None:  # usual sparse coding without radius restriction
                d = step_size * grad_norm
                step_size = (r / max(r, d)) * step_size

            if step_size * grad_norm / np.linalg.norm(H1_old, 2) > stopping_grad_ratio:
                H1[k, :] = H1[k, :] - step_size * grad
            else:
                if_continue[k] = 0  # stop making changes when negligible
                # print('!!! update skipped' )

            # print('!!! H1.shape', H1.shap
            if nonnegativity:
                H1[k,:] = np.maximum(H1[k,:], np.zeros(shape=(H1.shape[1],)))  # nonnegativity constraint

        i = i + 1

    return H1




def code_update_sparse(X, W, H0=None, r=None, alpha=1, sub_iter=[5], stopping_grad_ratio=0.02, subsample_ratio=None, nonnegativity=True):
    '''
    Find \hat{H} = argmin_H ( || X - WH||^2 ) within radius r from H0
    With constraint hoyer_sparseness(rows of H) = sparsity
    s(x) = (\sqrt{n} - |x|_{1}/|x|_{2}) / (\sqrt{n} - 1)
    For dictionary update, one can input X.T and H.T to get W.T with sparse columns of W
    '''

    # print('!!!! H0.shape', H0.shape)

    if H0 is None:
        H0 = np.random.rand(W.shape[1], X.shape[1])

    H1 = H0.copy()

    dist = 1

    idx = np.arange(X.shape[0])
    # print('!!! X.shape', X.shape)


    if (subsample_ratio is not None) and (X.shape[0]>X.shape[1]):
        idx = np.random.randint(X.shape[0], size=X.shape[0]//subsample_ratio)
        A = W[idx,:].T @ W[idx,:]
        B = W[idx,:].T @ X[idx,:]

    else:
        A = W[:,:].T @ W[:,:]
        B = W[:,:].T @ X[:,:]

    for k in [k for k in np.arange(H0.shape[0])]:
        # block-optimize each row to induce row-wise sparsity
        i = 0
        while (i < np.random.choice(sub_iter)):
            H1_old = H1.copy()
            # row-wise gradient descent
            n = H0.shape[1]
            # grad_sparseness = (1/(np.sqrt(n)-1)) * (np.ones(H0.shape[1])-2*np.linalg.norm(H0[k,:],1)*(np.linalg.norm(H0[k,:],1)**(-2/3))*H0[k,:])/np.linalg.norm(H0[k,:],2)
            grad = (np.dot(A[k, :], H1) - B[k, :] + alpha * np.ones(H0.shape[1]))
            grad_norm = np.linalg.norm(grad, 2)
            step_size = (1 / (((i + 2) ** (1)) * (A[k, k] + 1)))
            if r is not None:  # usual sparse coding without radius restriction
                d = step_size * grad_norm
                step_size = (r / max(r, d)) * step_size

            if step_size * grad_norm / np.linalg.norm(H1_old, 2) > stopping_grad_ratio:
                H1[k, :] = H1[k, :] - step_size * grad

            # print('!!! H1.shape', H1.shap
            if nonnegativity:
                H1[k,:] = np.maximum(H1[k,:], np.zeros(shape=(H1.shape[1],)))  # nonnegativity constraint

            i = i + 1


    """
    for k in np.arange(H0.shape[0]):
        # Do Hoyer's projection to induce sparsity
        # (\sqrt{n} - L1 / |x|_{2}) / (\sqrt{n} - 1) = sparseness
        x = H1[k,:]
        n = H1.shape[1]
        L2 = np.linalg.norm(x)
        L1 = (np.sqrt(n) - sparsity * (np.sqrt(n)-1))*np.linalg.norm(x)
        H1[k,:] = hoyer_projection(x.copy(), L1, L2)
    """


    return H1


def hoyer_projection(x, L1, L2, max_iter=100):
    """
    x (array) : input vector
    L1 (float) : L1 norm
    L2 (float) : L2 norm
    Given any vector x, find the closest (in the euclidean sense) non-negative vector s with a given L1 norm and a given L2 norm.
    Ref: P. Hoyer, "Non-negative Matrix Factorization with Sparseness Constraints", JMLR (2004)
    """

    # print('!!! np.size(x)', np.size(x))
    s = x + (L1 - np.linalg.norm(x,1))/np.size(x)
    Z = []
    if max_iter is None:
        max_iter = np.size(x)

    for j in np.arange(max_iter):
        # print('!!! np.linalg.norm(s,1)', np.linalg.norm(s,1))
        m = np.zeros(x.shape)
        for i in np.arange(np.size(x)):
            if i in Z:
                m[i] = L1/(np.size(x) - len(Z))
        # |m + a(s-m)|^2 = L2^2
        # |m|^2 + 2a<m,s-m> + a^2|s-m|^2 = L2^2
        # a = (-<m,s-m> +- np.sqrt( <m,s-m>^2 - |s-m|^2 (|m|^2 - L2^2)))/|s-m|^2
        disc = np.dot(m,s-m)**2 - (np.dot(s-m, s-m))*(np.dot(m,m) - L2**2)
        if disc<0:
            a = np.random.rand()
        else:
            a = (-np.dot(m, s-m) + np.sqrt( disc)) / np.dot(s-m,s-m)
        #print('!!! np.linalg.norm(m + a*(s-m),2)', np.linalg.norm(m + a*(s-m),2))
        #print('!!! L2', L2)
        s = m  + a*(s-m)

        if min(s) >= 0:
            break
        else:
            for i in np.arange(np.size(x)):
                if s[i]<0:
                    Z.append(i)
            for i in Z:
                s[i] = 0
            c = (np.linalg.norm(s,1) - L1)/(np.size(x) - len(Z))
            for i in np.arange(np.size(x)):
                if i not in Z:
                    s[i] = s[i] - c
        # print('!!! j', j)
    return s



def fit_MLR_GD(Y, H, W0=None, sub_iter=100, stopping_diff=0.01):
        '''
        Convex optimization algorithm for Multiclass Logistic Regression using Gradient Descent
        Y = (n x k), H = (p x n) (\Phi in lecture note), W = (p x k)
        Multiclass Logistic Regression: Y ~ vector of discrete RVs with PMF = sigmoid(H.T @ W)
        MLE -->
        Find \hat{W} = argmin_W ( sum_j ( log(1+exp(H_j.T @ W) ) - Y.T @ H.T @ W ) )
        '''
        k = Y.shape[1] # number of classes
        if W0 is None:
            W0 = np.random.rand(H.shape[0],k) #If initial coefficients W0 is None, randomly initialize

        W1 = W0.copy()
        i = 0
        grad = np.ones(W0.shape)
        while (i < sub_iter) and (np.linalg.norm(grad) > stopping_diff):
            Q = 1/(1+np.exp(-H.T @ W1))  # probability matrix, same shape as Y
            # grad = H @ (Q - Y).T + alpha * np.ones(W0.shape[1])
            grad = H @ (Q - Y)
            W1 = W1 - (np.log(i+1) / (((i + 1) ** (0.5)))) * grad
            i = i + 1
            # print('iter %i, grad_norm %f' %(i, np.linalg.norm(grad)))
        return W1
