''' Point forecasting classes with uncertanty estimation using MAPIE '''

import copy
import joblib
import pandas as pd, numpy as np, matplotlib.pyplot as plt
from datetime import datetime, timedelta
from sklearn.linear_model import ElasticNetCV, ElasticNet, Ridge
from mapie.regression import MapieRegressor
import xgboost as xgb
import shap
import logging
import holidays
from prophet import Prophet
from catboost import CatBoostRegressor
from xgboost import XGBRegressor
from lightgbm import LGBMRegressor

from logger import get_logger
logger = get_logger(__name__)



def detect_outliers_zscore(values, z_thresh: float = 3.0):
    """
    Return a boolean mask where True indicates the value is an outlier
    based on a simple z-score threshold.
    """
    series = pd.Series(values, dtype=float)  # ensure float
    mean_val = series.mean()
    std_val = series.std()
    if std_val == 0:
        # Avoid division by zero; no variation => no outliers
        return [False]*len(series)

    z_scores = (series - mean_val).abs() / std_val
    return z_scores > z_thresh

def detect_outliers_diff(values, diff_thresh: float = 10.0):
    """
    Return a boolean mask where True indicates the value is an outlier
    if it differs from the previous forecast by more than diff_thresh.
    """
    series = pd.Series(values, dtype=float)
    diffs = series.diff().abs()
    # For the first value, you can treat it as not outlier or handle it separately
    return diffs > diff_thresh

def fill_outliers_in_forecast_with_interpolation(
        array:np.ndarray, index:pd.DatetimeIndex, z_thresh:float=3.0, diff_thresh:float=100.,
        msg:str or None=None,
        verbose:bool=False
):

    array = np.array(array)

    # check for large outliers
    has_outliers = False
    outlier_mask = detect_outliers_zscore(array, z_thresh=z_thresh)
    if (len(array[outlier_mask]) > 0):
        array[outlier_mask] = np.nan
        if verbose: logger.warning (
            f"Outlier detection alert! "
            f"Method:z={z_thresh}-score "
            f"N={len(array[outlier_mask])} "
            f"msg={msg}"
        )

    outlier_mask = detect_outliers_diff(array, diff_thresh=diff_thresh)
    if (len(array[outlier_mask]) > 0):
        array[outlier_mask] = np.nan
        if verbose: logger.warning (
            f"Outlier detection alert! "
            f"Method:diff={diff_thresh} "
            f"N={len(array[outlier_mask])} "
            f"msg={msg}"
        )

    if np.isnan(array).any():
        series = pd.Series(array, index=index, dtype=float)
        forecast_series = series.interpolate(method='time')
        array = forecast_series.values

    return array

class BaseForecaster:

    ''' Uses point-wise regressor or forecaster to forecast several time-steps recursively '''
    def __init__(self, target:str, alpha:float=0.05, verbose:bool=False): # lags_target:int or None = 0,
        self.target = target
        self.alpha = alpha
        # self.lag_y_past : pd.Series = pd.Series()
        self.X_futures_df : pd.DataFrame = pd.DataFrame() # for feature importance
        # self.lags_target = lags_target
        self.trained_features = None
        self.model:MapieRegressor = MapieRegressor()
        self.name = 'BaseClass'
        self.verbose = verbose
        self.expected_mean_deviation = 20
        self.expected_std_deviation = 10
        self.train_end = None

    def _is_scaled(self, data):
        """
        Check if data is scaled: mean approximately 0, std approximately 1.
        """
        mean = data.mean()
        std = data.std()
        mean_check = (abs(mean) < self.expected_mean_deviation).all()  # Mean close to 0
        std_check = (abs(std - 1) < self.expected_std_deviation).all()  # Std close to 1
        return mean_check and std_check

    def get_regressor(self) -> MapieRegressor:
        return self.model

    def fit(self, X_scaled:pd.DataFrame, y_scaled:pd.DataFrame)->None:
        '''
        Fits the forecasting model to the data.
        :param X_scaled: time-series dataframe with exogenous variables. Normalized, regularized and scaled.
        :param y_scaled: time-series dataframe with target. Normalized and scaled.
        :return: None
        '''
        pass

    def predict(self, X_scaled:pd.DataFrame, y_scaled:pd.DataFrame or None) -> pd.DataFrame:
        '''
        Compute trained model predictions for each timestep in the given train data set.
        :param X_scaled: time-series dataframe with exogenous variables. (same as used for fitting)
        :param y_scaled: time-series dataframe with target. (same as used for fitting)
        :return: pd.DataFrame with columns:
            [f'{target}_actual',f'{target}_fitted',f'{target}_lower',f'{target}_upper'] where the last two
        correspond to 95% confidence intervals.
        '''
        # predict with model
        res, pis = self.model.predict(X_scaled, alpha=self.alpha)
        lower = np.array( pis[:, 0, 0] )
        upper = np.array( pis[:, 1, 0] )

        # form results
        if y_scaled is None:
            y_scaled = pd.DataFrame(
                {self.target:[np.nan] * len(X_scaled)}, columns=[self.target], index=X_scaled.index
            )

        array = np.array(res)
        # check for outliers (due to method failing)
        # array = fill_outliers_in_forecast_with_interpolation(
        #     np.array(res), index=y_scaled.index, verbose=self.verbose,
        #     msg=f" in 'predict' for target={self.target} method={self.name} (fitted)"
        # )
        # lower = fill_outliers_in_forecast_with_interpolation(
        #     np.array(lower), index=y_scaled.index, verbose=self.verbose,
        #     msg=f" in 'predict' for target={self.target} method={self.name} (lower)"
        # )
        # upper = fill_outliers_in_forecast_with_interpolation(
        #     np.array(upper), index=y_scaled.index, verbose=self.verbose,
        #     msg=f" in 'predict' for target={self.target} method={self.name} (upper)"
        # )

        results = pd.DataFrame({
            f'{self.target}_actual': y_scaled[self.target].values,
            f'{self.target}_fitted': pd.Series(array, index=y_scaled.index),
            f'{self.target}_lower': pd.Series(lower, index=y_scaled.index),
            f'{self.target}_upper': pd.Series(upper, index=y_scaled.index)
        }, index=X_scaled.index)
        return results

    def forecast_window(self, X_test:pd.DataFrame, y_train_scaled:pd.DataFrame or None, lags_target:int or None)->pd.DataFrame:
        '''
        Compute trained model forecast for each timestep in the given test data set.
        If there are lagged targets as features, then iterative procedure is requried. Otherwise it is
        equivalent to calling predict()

        :param X_scaled: time-series dataframe with exogenous variables. (e.g., forecasting horizon length)
        :return: pd.DataFrame with columns:
        [f'{target}_actual',f'{target}_fitted',f'{target}_lower',f'{target}_upper'] where the last two
        correspond to 95% confidence intervals.
        '''

        # if X_test.index[0] < self.train_end:
        #     raise ValueError(f"Data leakage X_test[0] = {X_test.index[0]} while training data is {self.train_end}")

        if lags_target is None:
            return self.predict(X_test, None)

        if not lags_target is None:
            if y_train_scaled is None:
                raise ValueError("Requires y_train_scaled for lagged target")
            time_delta = pd.Timedelta(y_train_scaled.index[-1] - y_train_scaled.index[-2])
            if y_train_scaled.index[-1] + time_delta != X_test.index[0]:
                raise ValueError("X_test.inex[0] must be y_train.inex[0] + 1 hour. Given "
                                 f"y_train_scaled.index[-1] + 1 = {y_train_scaled.index[-1] + timedelta(hours=1)}"
                                 f"X_test.index[0] = {X_test.index[0]}")

        # if set(X_test.columns) != set(self.features):
        #     raise ValueError("X_test.columns must match self.features")

        if len(y_train_scaled.columns) > 1:
            raise ValueError("y_train_scaled.columns must have 1 column for single target forecasting")

        # for simplicity
        y_train_scaled = pd.Series(y_train_scaled[self.target].values, index=y_train_scaled.index)

        forecast_values, lower, upper, X_futures = [], [], [], []
        # predict target for each time step in future features X_scaled
        for i in range(len(X_test)):
            # X_future = self.ds.exog_forecast.iloc[[i]].to_dict('records')[0]
            X_future = X_test.iloc[[i]].to_dict('records')[0]
            # Update lag features for target (if lag featires are needed)
            if not lags_target is None:
                for lag in range(1, lags_target+1):
                    if not f'{self.target}_lag_{lag}' in X_test.columns:
                        raise ValueError("X_test.columns must match self.features")
                    if (i - lag >= 0):
                        # For the target variable, use forecasted values
                        X_future[f'{self.target}_lag_{lag}'] = forecast_values[i - lag]
                    else:
                        # Use historical data for initial lags
                        val = y_train_scaled.iloc[-lag + i]
                        X_future[f'{self.target}_lag_{lag}'] = val
            # Predict and store forecasted value
            values = pd.DataFrame([X_future], columns=X_future.keys())
            # Save X_future for feature importance
            X_futures.append(values.copy(deep=True))
            # use MapieRegressor.predict() to forecast for the next time-step
            forecast_value, forecast_pis = self.get_regressor().predict( values, alpha=self.alpha )
            forecast_values.append(forecast_value[0])
            # get confidence intervals
            lower.append(forecast_pis[:, 0, 0][0])
            upper.append(forecast_pis[:, 1, 0][0])

        # check for nans
        array = np.array(forecast_values)  # Ensure input is a NumPy array
        if np.isnan(array).any():
            nan_count = np.isnan(array).sum()
            raise ValueError(f"Forecast window contains NaN values: {nan_count} for {self.target}")
        if np.isinf(array).any():
            inf_count = np.isinf(array).sum()
            raise ValueError(f"Forecast window contains inf values: {inf_count} for {self.target}")


        # Save x_futures for later use Convert x_futures to DataFrame
        self.X_futures_df = pd.concat(X_futures, axis=0, ignore_index=True)
        self.X_futures_df.index = X_test.index

        # combine the result of the forecast
        df = pd.DataFrame({
            f'{self.target}_actual':[np.nan]*len(forecast_values),
            f'{self.target}_fitted': pd.Series(forecast_values, index=X_test.index),
            f'{self.target}_lower': pd.Series(lower, index=X_test.index),
            f'{self.target}_upper': pd.Series(upper, index=X_test.index)
        }, index=X_test.index)

        return df

    def get_model_feature_importance(self, X_train_scaled, y_train_scaled, X_test_scaled, lags_target:int or None)->pd.DataFrame:
        '''
        Compute feature importance using Shapley algorithm or other methods.
        :param X_scaled: time-series dataframe with exogenous variables. (e.g., forecasting horizon length)
        :return: pd.DataFrame with importances for each feature and each timestep
        '''

        _ = self.forecast_window(X_test_scaled, y_train_scaled, lags_target) # populate self.X_futures_df:pd.Dataframe

        mapie_regressor : MapieRegressor = self.get_regressor()
        ensemble_estimator = mapie_regressor.estimator_


        if hasattr(ensemble_estimator, 'estimators_'):
            # Collect SHAP values from each estimator
            shap_values_list = []
            for est in ensemble_estimator.estimators_:
                if isinstance(est, xgb.XGBRegressor):
                    explainer = shap.TreeExplainer(est)
                    shap_values = explainer.shap_values(self.X_futures_df)
                elif isinstance(est, ElasticNet):
                    # Use LinearExplainer for ElasticNet
                    explainer = shap.LinearExplainer(est, X_train_scaled)
                    shap_values = explainer.shap_values(self.X_futures_df)
                else:
                    # Use KernelExplainer for Unknown regressor
                    logger.warning(f"Using slow KernelExplainer() for estimator={est}")
                    background = X_train_scaled if len(X_train_scaled) <= 100 \
                        else X_train_scaled.sample(250, random_state=0)
                    explainer = shap.KernelExplainer(est.predict, background)
                    shap_values = explainer.shap_values(self.X_futures_df)
                shap_values_list.append(shap_values)

            # Average SHAP values across estimators
            shap_values_avg = np.mean(shap_values_list, axis=0)
        else:
            raise AttributeError("No estimators_ attribute found in the ensemble estimator.")

        # Return SHAP values as a DataFrame (indexed with pd.Timestamp for 150 timesteps)
        shap_df = pd.DataFrame(shap_values_avg, columns=self.X_futures_df.columns, index=self.X_futures_df.index)
        return shap_df

    def save_model(self, file_path: str):
        """
        Save the trained model to a file using joblib.
        """
        # if hasattr(self.model.estimator, "fit"):
        #     raise ValueError("The model does not appear to be trained.")

        joblib.dump(self.model, file_path)
        # print(f"Model saved to {file_path}")

    def load_model(self, file_path: str):
        """
        Load a trained model from a file using joblib.
        """
        self.model = joblib.load(file_path)
        # print(f"Model loaded from {file_path}")

    def reset_model(self):
        del self.model; self.model = MapieRegressor()
        del self.X_futures_df;  self.X_futures_df = pd.DataFrame()

class XGBoostMapieRegressor(BaseForecaster):

    def __init__(self,target:str,  model: MapieRegressor, alpha:float, verbose:bool): # lags_target:int or None,
        super().__init__(
            target, alpha, verbose
        )
        self.name='XGBoostMapieRegressor'
        self.model = model

    def fit(self, X_scaled:pd.DataFrame, y_scaled:pd.DataFrame):
        # Check if base model is pre-fitted
        if hasattr(self.model.estimator, "fit"):
            if self.verbose: logger.info(f"Base model {self.name} is not fitted. Fitting using X={X_scaled.shape}")
            self.model.estimator.fit(X_scaled, y_scaled)
        if len(X_scaled) == 0 or len(y_scaled) == 0:
            raise ValueError(
                f"Empty dataframe is passed for training: "
                f"X_scaled={X_scaled.shape} and y_scaled={y_scaled.shape}"
            )
        # fit the Mapieregressor model
        y_scaled = pd.Series(y_scaled.values.flatten(), index=y_scaled.index)
        self.model.fit(X_scaled, y_scaled)
        self.train_end = y_scaled.index[-1]

class LGBMMapieRegressor(BaseForecaster):

    def __init__(self,target:str,  model: MapieRegressor, alpha:float, verbose:bool): # lags_target:int or None,
        super().__init__(
            target, alpha, verbose
        )
        self.name='LGBMMapieRegressor'
        self.model = model

    def fit(self, X_scaled:pd.DataFrame, y_scaled:pd.DataFrame):
        # Check if base model is pre-fitted
        if hasattr(self.model.estimator, "fit"):
            if self.verbose: logger.info(f"Base model {self.name} is not fitted. Fitting using X={X_scaled.shape}")
            self.model.estimator.fit(X_scaled, y_scaled)
        if len(X_scaled) == 0 or len(y_scaled) == 0:
            raise ValueError(
                f"Empty dataframe is passed for training: "
                f"X_scaled={X_scaled.shape} and y_scaled={y_scaled.shape}"
            )
        # fit the Mapieregressor model
        y_scaled = pd.Series(y_scaled.values.flatten(), index=y_scaled.index)
        self.model.fit(X_scaled, y_scaled)
        self.train_end = y_scaled.index[-1]


class ProphetForecaster(BaseForecaster):

    def __init__(self, params:dict,target:str, alpha:float, verbose:bool):
        super().__init__(
            target, alpha, verbose
        )
        self.params = copy.deepcopy(params)
        self.name='Prophet'
        self.target=target

        self.forecast_result = None


    def _create_prophet_df(self, X_scaled:pd.DataFrame, y_scaled:pd.Series)->pd.DataFrame:

        prophet_df = pd.DataFrame(
            columns=['ds', 'y']
        )
        prophet_df['y'] = y_scaled
        prophet_df['ds'] = y_scaled.index

        # Remove Timezone Information
        if prophet_df['ds'].dt.tz is not None:
            prophet_df['ds'] = prophet_df['ds'].dt.tz_localize(None)

        # add exogenous variables to prophet
        for col in X_scaled.columns.to_list():
            prophet_df[str(col)] = X_scaled[col].values
        return prophet_df

    def fit(self, X_scaled:pd.DataFrame, y_scaled:pd.DataFrame):
        if len(X_scaled) == 0 or len(y_scaled) == 0:
            raise ValueError(
                f"Empty dataframe is passed for training: "
                f"X_scaled={X_scaled.shape} and y_scaled={y_scaled.shape}"
            )
        '''
        :X_scaled indexed by pd.Timestamp
        :y_scaled indexed by pd.Timestamp
        '''

        if len(y_scaled.columns) > 1:
            raise ValueError("y_train_scaled.columns must have 1 column for single target forecasting")
        # for simplicity
        y_scaled = pd.Series(y_scaled[self.target].values, index=y_scaled.index)

        # Check if datasets are scaled
        if not self._is_scaled(X_scaled):
            raise ValueError(f"The dataset X_scaled is not properly scaled. "
                             f"Found mean or std > {self.expected_std_deviation}")
        if not self._is_scaled(y_scaled):
            raise ValueError("The dataset y_scaled is not properly scaled. "
                             f"Found mean or std > {self.expected_std_deviation}")


        self.prophet_df = self._create_prophet_df(X_scaled, y_scaled)

        # add interval width for prediction interval inference
        params = self.params | {'interval_width':1-self.alpha}
        # Initialize the model with the same hyperparameters
        self.model = Prophet(**params)
        # include exogenous variables as regressors
        self.model.add_country_holidays('Germany')

        # add exogenous features
        for col in X_scaled.columns.to_list():
            self.model.add_regressor(str(col))

        # fit the model
        logger = logging.getLogger('cmdstanpy')
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
        logger.setLevel(logging.CRITICAL)

        if self.verbose: logger.info(f"Base model {self.name} is not fitted. Fitting using X={X_scaled.shape}")
        self.model.fit(self.prophet_df)


    def predict(self, X_scaled:pd.DataFrame, y_scaled:pd.DataFrame) -> pd.DataFrame:
        ''' predict target for all entries in the training data '''
        if len(y_scaled.columns) > 1:
            raise ValueError("y_train_scaled.columns must have 1 column for single target forecasting")
        # for simplicity
        y_scaled = pd.Series(y_scaled[self.target].values, index=y_scaled.index)

        prophet_df = self._create_prophet_df(X_scaled, y_scaled)
        # Generate predictions
        result:pd.DataFrame = self.model.predict(prophet_df)#self.forecaster.predict_for_train() # [['ds']]

        # Prepare output DataFrame
        predict_df = pd.DataFrame({
            'date': result['ds'].values,
            f'{self.target}_actual': y_scaled.values,
            f'{self.target}_fitted': result['yhat'].values,
            f'{self.target}_lower': result['yhat_lower'].values,
            f'{self.target}_upper': result['yhat_upper'].values
        }).set_index('date')
        predict_df.index = predict_df.index.tz_localize(y_scaled.index.tzinfo)

        return predict_df

    def forecast_window(self, X_scaled:pd.DataFrame,y_train_scaled:pd.DataFrame or None,lags_target:int or None)->pd.DataFrame:
        ''' Overrides base method. Lagged features are not supported for Prophet model.
        '''

        if len(y_train_scaled.columns) > 1:
            raise ValueError("y_train_scaled.columns must have 1 column for single target forecasting")
        # for simplicity
        y_train_scaled = pd.Series(y_train_scaled[self.target].values, index=y_train_scaled.index)

        # self.X_future = X_scaled
            # Generate predictions
        predict_df = pd.DataFrame(index=X_scaled.index)
        predict_df.index.name = 'date'
        predict_df.reset_index(inplace=True, names='ds')
        if predict_df['ds'].dt.tz is not None:
            predict_df['ds'] = predict_df['ds'].dt.tz_localize(None)

        # add forecasted exogenous variables to prophet (same as added regressors)
        for col in X_scaled.columns.tolist():
            predict_df[str(col)] = X_scaled[col].values

        self.forecast_result:pd.DataFrame = self.model.predict(predict_df) # where model is Prophet() with several model.add_regressor(str(col))

        # Prepare output DataFrame
        predict_df = pd.DataFrame({
            'date': self.forecast_result['ds'].values,
            f'{self.target}_actual': np.nan,
            f'{self.target}_fitted': self.forecast_result['yhat'].values,
            f'{self.target}_lower': self.forecast_result['yhat_lower'].values,
            f'{self.target}_upper': self.forecast_result['yhat_upper'].values
        }).set_index('date')
        predict_df.index = predict_df.index.tz_localize(X_scaled.index.tzinfo)
        return predict_df

    def get_model_feature_importance(self, X_train:pd.DataFrame, y_train:pd.DataFrame, X_test:pd.DataFrame,lags_target:int or None) -> pd.DataFrame:
        """
        Explanation
        Prophet provides the additive contribution of each regressor, which directly reflects their
        importance in the model's predictions.

        Extract Regressor Contributions: We extract the columns corresponding to the regressors from
        self.forecast_result. These columns contain the additive contributions of each regressor at each timestep.
        Centering the Contributions: By subtracting the mean contribution of each regressor,
        we center the contributions around zero. This step makes the contributions interpretable similarly to
        SHAP values, which represent the deviation from the average prediction.
        Centering the contributions makes them more interpretable, allowing us to see how each regressor's
        contribution deviates from its average effect, similar to SHAP values.

        Resulting DataFrame: The returned DataFrame has timestamps as the index and regressors as columns,
        with values representing the centered contributions of each regressor at each timestep.

        :return: pd.DataFrame with feature importances for each forecasted timestep and each regressor.
        """
        # Get the list of regressor names
        regressor_names = list(self.model.extra_regressors.keys())

        # Extract the contributions of the regressors
        regressor_contributions = self.forecast_result[regressor_names].copy()

        # Center the contributions by subtracting the mean (like SHAP values)
        mean_contributions = regressor_contributions.mean()
        shap_like_values = regressor_contributions - mean_contributions

        # Add the timestamp as the index
        shap_like_values.index = self.forecast_result['ds']
        shap_like_values.index = shap_like_values.index.tz_localize(X_test.index.tzinfo)
        shap_like_values.index.name = 'date'
        return shap_like_values

class ElasticNetMapieRegressor(BaseForecaster):

    def __init__(self,
                 target:str, model:MapieRegressor, alpha:float, verbose:bool # lags_target:int or None,
                 ):
        super().__init__(
            target, alpha, verbose
        )
        self.name = "ElasticNetMapieRegressor"
        self.base_model = model
        self.features = None

    def fit(self, X_scaled:pd.DataFrame, y_scaled:pd.DataFrame) -> None:
        # Check if base model is pre-fitted
        if hasattr(self.model.estimator, "fit"):
            if self.verbose: logger.info(f"Base model {self.name} is not fitted. Fitting using X={X_scaled.shape}")
            # print("Base model has a 'fit' method and is likely not pre-fitted.")
            self.model.estimator.fit(X_scaled, y_scaled)

        if len(X_scaled) == 0 or len(y_scaled) == 0:
            raise ValueError(
                f"Empty dataframe is passed for training: "
                f"X_scaled={X_scaled.shape} and y_scaled={y_scaled.shape}"
            )

        # Fit mapieregressor model
        y_scaled = pd.Series(y_scaled[self.target],name=self.target,index=y_scaled.index)
        self.model.fit(X_scaled, y_scaled)
        # self.features = X_scaled.columns.tolist()
        # self.lag_y_past = y_scaled.copy()

    # def get_model_feature_importance(self)->pd.Series:
    #     ''' get feature importance from MapieRegressor (of ElasticNet) '''
    #
    #     # Ensure the MapieRegressor model and its base estimator are fitted
    #     if not hasattr(self.model, 'estimator_'):
    #         raise AttributeError("The MapieRegressor model has not been fitted or is not accessible.")
    #
    #     # Access the underlying ensemble estimator
    #     mapie_regressor = self.get_regressor()
    #     ensemble_estimator = mapie_regressor.estimator_
    #
    #     # Check if the base estimator has an ensemble of models (e.g., in a bagging setup)
    #     if hasattr(ensemble_estimator, 'estimators_'):
    #         # Initialize a list to collect coefficients from each estimator
    #         coef_list = []
    #
    #         # Iterate through each estimator to collect coefficients
    #         for est in ensemble_estimator.estimators_:
    #             if hasattr(est, 'coef_'):
    #                 coef_list.append(est.coef_)
    #             else:
    #                 raise AttributeError("An estimator in the ensemble lacks `coef_` attributes.")
    #
    #         # Calculate the mean of the coefficients across estimators
    #         avg_coefficients = np.mean(coef_list, axis=0)
    #     elif hasattr(ensemble_estimator, 'coef_'):
    #         # If there's only a single estimator, use its coefficients directly
    #         avg_coefficients = ensemble_estimator.coef_
    #     else:
    #         raise AttributeError("No `coef_` attribute found in the ensemble or base model.")
    #
    #     # Create a Series of averaged coefficients for feature importance
    #     feature_importance = pd.Series(avg_coefficients, index=self.features)
    #     feature_importance = feature_importance.abs().sort_values(ascending=False)
    #     return feature_importance


def instantiate_base_singletarget_forecaster(model_name:str, targets:list, model_pars:dict, verbose:bool)->BaseForecaster:
    # if 'l1_ratio' in model_pars: del model_pars['l1_ratio']
    # train the forecasting model several times to evaluate its performance, get all results
    if model_name == 'XGBoost':
        if len(targets) > 1: raise ValueError("XGBoost does not support multiple targets")
        return XGBoostMapieRegressor(
            model=MapieRegressor(
                xgb.XGBRegressor(**model_pars),
                method='naive', cv='prefit'#TimeSeriesSplit(n_splits=5)
            ), target=targets[0], alpha=0.05, verbose=verbose)

    if model_name == 'LightGBM':
        extra_pars = {'importance_type': 'gain', "verbose":-1} # Use 'gain' importance for feature selection
        if len(targets) > 1: raise ValueError("LightGBM does not support multiple targets")
        return LGBMMapieRegressor(
            model=MapieRegressor(
                LGBMRegressor(**(model_pars | extra_pars)),
                method='naive', cv='prefit'#TimeSeriesSplit(n_splits=5)
            ), target=targets[0], alpha=0.05, verbose=verbose)

    elif model_name == 'ElasticNet':
        if len(targets) > 1: raise ValueError("ElasticNet does not support multiple targets")
        return ElasticNetMapieRegressor(
            model=MapieRegressor(
                ElasticNet(**(model_pars | { 'max_iter':1e6, 'tol':1e-8 })),
                method='naive', cv='prefit'#TimeSeriesSplit(n_splits=5)
            ), target=targets[0], alpha=0.05, verbose=verbose)

    elif model_name == 'Prophet':
        if len(targets) > 1: raise ValueError("Prophet does not support multiple targets")
        return ProphetForecaster( params = model_pars, target=targets[0], alpha=0.05, verbose=verbose)

    else:
        raise NotImplementedError(f"Fine-tuning parameter set for {model_name} not implemented")