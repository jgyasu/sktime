# copyright: sktime developers, BSD-3-Clause License (see LICENSE file)
"""Implements EnbPIForecaster."""

import numpy as np
import pandas as pd
from sklearn.base import clone
from sklearn.utils import check_random_state

from sktime.forecasting.base import BaseForecaster
from sktime.forecasting.naive import NaiveForecaster
from sktime.libs._aws_fortuna_enbpi.enbpi import EnbPI
from sktime.transformations.bootstrap import (
    MovingBlockBootstrapTransformer,
    TSBootstrapAdapter,
)
from sktime.utils.dependencies._dependencies import _check_soft_dependencies
from sktime.utils.warnings import warn

__all__ = ["EnbPIForecaster"]
__author__ = ["benheid"]


class EnbPIForecaster(BaseForecaster):
    """
    Ensemble Bootstrap Prediction Interval Forecaster.

    The forecaster combines sktime forecasters, with tsbootstrap bootstrappers
    and the EnbPI algorithm [1] implemented in fortuna using the
    tutorial from this blogpost [2].

    The forecaster is similar to the the bagging forecaster and performs
    internally the following steps.

    For training:

        1. Uses a bootstrap transformer to generate bootstrap samples
           and returning the corresponding indices of the original time
           series. Note that the bootstrap transformer must be able to
           return indices of the original time series as and additional column.
           I.e., the ``bootstrap_transformer`` must have the
           ``capability:bootstrap_indices`` tag, and its parameter
           ``return_indices`` must be set to True.
        2. Fit a forecaster on the first n - max(fh) values of each
           bootstrap sample
        3. Uses each forecaster to predict the last max(fh) values of each
           bootstrap sample

    For Prediction:

        1. Average the predictions of each fitted forecaster using the
           aggregation function

    For Probabilistic Forecasting:

        1. Calculate the point forecast by average the prediction of each
           fitted forecaster using the aggregation function
        2. Passes the indices of the bootstrapped samples, the predictions
           from the fit call, the point prediction of the test set, and
           the desired error rate to the EnbPI algorithm to calculate the
           prediction intervals.
           For more information on the EnbPI algorithm, see the references
           and the documentation of the EnbPI class in aws-fortuna.

    Parameters
    ----------
    forecaster : estimator
        The base forecaster to fit to each bootstrap sample.
    bootstrap_transformer : tsbootstrap.BootstrapTransformer
        The transformer to fit to the target series to generate bootstrap samples.
        This transformer must be able to return the indices of the original
        time series as an additional column. I.e., the ``bootstrap_transformer``
        must have the
        ``capability:bootstrap_indices`` tag, and its parameter
        ``return_indices`` must be set to True.
    random_state : int, RandomState instance or None, default=None
        Random state for reproducibility.
    aggregation_function : str, default="mean"
        The aggregation function to use for combining the predictions of the
        fitted forecasters. Either "mean" or "median".

    Examples
    --------
    >>> import numpy as np
    >>> from tsbootstrap import MovingBlockBootstrap
    >>> from sktime.forecasting.enbpi import EnbPIForecaster
    >>> from sktime.forecasting.naive import NaiveForecaster
    >>> from sktime.datasets import load_airline
    >>> from sktime.transformations.series.difference import Differencer
    >>> from sktime.transformations.series.detrend import Deseasonalizer
    >>> from sktime.forecasting.base import ForecastingHorizon
    >>> y = load_airline()
    >>> forecaster = Differencer(lags=[1]) * Deseasonalizer(sp=12) * EnbPIForecaster(
    ...    forecaster=NaiveForecaster(sp=12),
    ...    bootstrap_transformer=MovingBlockBootstrap(n_bootstraps=10))
    >>> fh = ForecastingHorizon(np.arange(1, 13))
    >>> forecaster.fit(y, fh=fh)
    TransformedTargetForecaster(...)
    >>> res = forecaster.predict()
    >>> res_int = forecaster.predict_interval(coverage=[0.5])

    References
    ----------
    .. [1] Chen Xu & Yao Xie (2021). Conformal Prediction Interval for Dynamic
    Time-Series.
    .. [2] Valeriy Manokhin, PhD, MBA, CQF. Demystifying EnbPI: Mastering Conformal
    Prediction Forecasting
    """

    _tags = {
        "authors": ["benheid"],
        "python_dependencies": ["tsbootstrap>=0.1.0"],
        "scitype:y": "univariate",  # which y are fine? univariate/multivariate/both
        "ignores-exogeneous-X": False,  # does estimator ignore the exogeneous X?
        "capability:missing_values": False,  # can estimator handle missing data?
        "y_inner_mtype": "pd.DataFrame",
        # which types do _fit, _predict, assume for y?
        "X_inner_mtype": "pd.DataFrame",
        # which types do _fit, _predict, assume for X?
        "X-y-must-have-same-index": True,  # can estimator handle different X/y index?
        "requires-fh-in-fit": True,  # like AutoETS overwritten if forecaster not None
        "enforce_index_type": None,  # like AutoETS overwritten if forecaster not None
        "capability:insample": False,  # can the estimator make in-sample predictions?
        "capability:pred_int": True,  # can the estimator produce prediction intervals?
        "capability:pred_int:insample": False,  # ... for in-sample horizons?
    }

    def __init__(
        self,
        forecaster=None,
        bootstrap_transformer=None,
        random_state=None,
        aggregation_function="mean",
    ):
        self.forecaster = forecaster
        self.forecaster_ = (
            forecaster.clone() if forecaster is not None else NaiveForecaster()
        )
        self.bootstrap_transformer = bootstrap_transformer
        self.random_state = random_state
        self.aggregation_function = aggregation_function
        if self.aggregation_function == "mean":
            self._aggregation_function = np.mean
        elif self.aggregation_function == "median":
            self._aggregation_function = np.median
        else:
            raise ValueError(
                f"Aggregation function {self.aggregation_function} not supported. "
                f"Please choose either 'mean' or 'median'."
            )

        super().__init__()

        if bootstrap_transformer.get_tag("object_type") == "bootstrap":
            self.bootstrap_transformer_ = TSBootstrapAdapter(
                bootstrap_transformer, return_indices=True
            )
        else:
            self.bootstrap_transformer_ = bootstrap_transformer

        if self.bootstrap_transformer is None:
            # todo 0.39.0: remove this warning
            warn(
                "The default value for the bootstrap_transformer will change to the"
                "sktime MovingBlockBootstrap in version 0.39.0."
                "For obtaining the current default behaviour after 0.39.0, pass "
                "bootstrap_transformer=TSBootstrapAdapter(MovingBlockBootstrap()), "
                "with moving block bootstrap from tsbootstrap.",
                obj=self,
                stacklevel=2,
            )
            from tsbootstrap import MovingBlockBootstrap

            # todo 0.39.0: replace with Moving Block Bootstrap from sktime. And set
            # the return_indices=True
            self.bootstrap_transformer_ = TSBootstrapAdapter(MovingBlockBootstrap())

        bs_capable = self.bootstrap_transformer_.get_tag(
            "capability:bootstrap_index", False, raise_error=False
        )
        if not bs_capable or not self.bootstrap_transformer_.return_indices:
            raise ValueError(
                "Error in EnbPIForecaster: "
                "The bootstrap_transformer needs to be able to "
                "return bootstrap indices, i.e., it must have the tag "
                "'capability:bootstrap_index' and the parameter "
                "'return_indices' must be set to True."
            )

    def _fit(self, X, y, fh=None):
        self._fh = fh
        self._y_ix_names = y.index.names

        # random state handling passed into input estimators
        self.random_state_ = check_random_state(self.random_state)

        # fit/transform the transformer to obtain bootstrap samples
        bs_ts_index = self.bootstrap_transformer_.fit_transform(y)

        self.indexes = bs_ts_index["resampled_index"].values.reshape((-1, len(y)))
        bootstrapped_ts = bs_ts_index[y.columns]

        self.forecasters = []
        self._preds = []
        # Fit Models per Bootstrap Sample
        for bs_index in bootstrapped_ts.index.get_level_values(0).unique():
            bs_ts = bootstrapped_ts.loc[bs_index]
            bs_df = pd.DataFrame(bs_ts, index=y.index)
            forecaster = clone(self.forecaster_)
            forecaster.fit(y=bs_df, fh=fh, X=X)
            self.forecasters.append(forecaster)
            prediction = forecaster.predict(fh=y.index, X=X)
            self._preds.append(prediction)

        return self

    def _predict(self, X, fh=None):
        # Calculate Prediction Intervals using Bootstrap Samples

        preds = [forecaster.predict(fh=fh, X=X) for forecaster in self.forecasters]

        return pd.DataFrame(
            self._aggregation_function(np.stack(preds, axis=0), axis=0),
            index=list(fh.to_absolute(self.cutoff)),
            columns=self._y.columns,
        )

    def _predict_interval(self, fh, X, coverage):
        preds = []
        for forecaster in self.forecasters:
            preds.append(forecaster.predict(fh=fh, X=X).values)

        train_targets = self._y.copy()
        train_targets.index = pd.RangeIndex(len(train_targets))
        intervals = []
        for cov in coverage:
            conformal_intervals = EnbPI(self.aggregation_function).conformal_interval(
                bootstrap_indices=self.indexes,
                bootstrap_train_preds=np.stack(self._preds),
                bootstrap_test_preds=np.stack(preds),
                train_targets=train_targets.values,
                error=1 - cov,
            )
            intervals.append(conformal_intervals.reshape(-1, 2))

        cols = pd.MultiIndex.from_product(
            [self._y.columns, coverage, ["lower", "upper"]]
        )
        fh_absolute_idx = fh.to_absolute_index(self.cutoff)
        pred_int = pd.DataFrame(
            np.concatenate(intervals, axis=1), index=fh_absolute_idx, columns=cols
        )
        return pred_int

    def _update(self, y, X=None, update_params=True):
        """Update cutoff value and, optionally, fitted parameters.

        Parameters
        ----------
        y : pd.Series, pd.DataFrame, or np.array
            Target time series to which to fit the forecaster.
        X : pd.DataFrame, optional (default=None)
            Exogeneous data
        update_params : bool, optional (default=True)
            whether model parameters should be updated

        Returns
        -------
        self : reference to self
        """
        self.fit(y=self._y, X=self._X, fh=self._fh)
        return self

    @classmethod
    def get_test_params(cls):
        """Return testing parameter settings for the estimator.

        Returns
        -------
        params : dict or list of dict, default = {}
            Parameters to create testing instances of the class
            Each dict are parameters to construct an "interesting" test instance, i.e.,
            ``MyClass(**params)`` or ``MyClass(**params[i])`` creates a valid test
            instance.
            ``create_test_instance`` uses the first (or only) dictionary in ``params``
        """
        params = [
            {
                "bootstrap_transformer": MovingBlockBootstrapTransformer(
                    return_indices=True
                ),
            }
        ]
        if _check_soft_dependencies("tsbootstrap", severity="none"):
            from tsbootstrap import BlockBootstrap

            params.append(
                {
                    "forecaster": NaiveForecaster(),
                    "bootstrap_transformer": BlockBootstrap(),
                }
            )

        return params
