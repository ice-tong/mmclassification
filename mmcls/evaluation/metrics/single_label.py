# Copyright (c) OpenMMLab. All rights reserved.
import warnings
from typing import Dict, List, Optional, Sequence, Union

import mmengine
import mmeval
import numpy as np
import torch
import torch.nn.functional as F
from mmengine.evaluator import BaseMetric

from mmcls.registry import METRICS


def to_tensor(value):
    """Convert value to torch.Tensor."""
    if isinstance(value, np.ndarray):
        value = torch.from_numpy(value)
    elif isinstance(value, Sequence) and not mmengine.is_str(value):
        value = torch.tensor(value)
    elif not isinstance(value, torch.Tensor):
        raise TypeError(f'{type(value)} is not an available argument.')
    return value


def _precision_recall_f1_support(pred_positive, gt_positive, average):
    """calculate base classification task metrics, such as  precision, recall,
    f1_score, support."""
    average_options = ['micro', 'macro', None]
    assert average in average_options, 'Invalid `average` argument, ' \
        f'please specicy from {average_options}.'

    class_correct = (pred_positive & gt_positive)
    if average == 'micro':
        tp_sum = class_correct.sum()
        pred_sum = pred_positive.sum()
        gt_sum = gt_positive.sum()
    else:
        tp_sum = class_correct.sum(0)
        pred_sum = pred_positive.sum(0)
        gt_sum = gt_positive.sum(0)

    precision = tp_sum / torch.clamp(pred_sum, min=1).float() * 100
    recall = tp_sum / torch.clamp(gt_sum, min=1).float() * 100
    f1_score = 2 * precision * recall / torch.clamp(
        precision + recall, min=torch.finfo(torch.float32).eps)
    if average in ['macro', 'micro']:
        precision = precision.mean(0)
        recall = recall.mean(0)
        f1_score = f1_score.mean(0)
        support = gt_sum.sum(0)
    else:
        support = gt_sum
    return precision, recall, f1_score, support


@METRICS.register_module()
class Accuracy(mmeval.Accuracy):
    """A wrapper of class:`mmeval.Accuracy`.

    Args:
        topk (int | Sequence[int]): If the predictions in ``topk``
            matches the target, the predictions will be regarded as
            correct ones. Defaults to 1.
        thrs (Sequence[float | None] | float | None): Predictions with scores
            under the thresholds are considered negative. None means no
            thresholds. Defaults to 0.
        dist_backend (str | None): The name of the distributed communication
            backend. Refer to :class:`mmeval.BaseMetric`.
            Defaults to 'torch_cuda'.
        **kwargs: Keyword parameters passed to :class:`mmeval.Accuracy`.

    Examples:
        >>> import torch
        >>> from mmcls.evaluation import Accuracy
        >>> # -------------------- The Basic Usage --------------------
        >>> accuracy = Accuracy()
        >>> y_pred = [0, 2, 1, 3]
        >>> y_true = [0, 1, 2, 3]
        >>> accuracy.calculate(y_pred, y_true)
        tensor([50.])
        >>> # Calculate the top1 and top5 accuracy.
        >>> accuracy = Accuracy(topk=(1, 5))
        >>> y_score = torch.rand((1000, 10))
        >>> y_true = torch.zeros((1000, ))
        >>> accuracy.calculate(y_score, y_true)  # doctest: +ELLIPSIS
        [[tensor([...])], [tensor([...])]]
        >>>
        >>> # ------------------- Use with Evalutor -------------------
        >>> from mmcls.structures import ClsDataSample
        >>> from mmengine.evaluator import Evaluator
        >>> data_samples = [
        ...     ClsDataSample().set_gt_label(0).set_pred_score(torch.rand(10))
        ...     for i in range(1000)
        ... ]
        >>> evaluator = Evaluator(metrics=Accuracy(topk=(1, 5)))
        >>> evaluator.process(data_samples)
        >>> evaluator.evaluate(1000)  # doctest: +ELLIPSIS
        {
            'Accuracy/top1': ...,
            'Accuracy/top5': ...
        }
    """

    def __init__(self,
                 topk: Union[int, Sequence[int]] = (1, ),
                 thrs: Union[float, Sequence[Union[float, None]], None] = 0.,
                 dist_backend: str = 'torch_cuda',
                 **kwargs) -> None:
        prefix = kwargs.pop('prefix', None)
        if prefix is not None:
            warnings.warn(
                'DeprecationWarning: The `prefix` parameter of `Accuracy`'
                ' is deprecated.')

        collect_device = kwargs.pop('collect_device', None)
        if collect_device is not None:
            warnings.warn(
                'DeprecationWarning: The `collect_device` parameter of '
                '`Accuracy` is deprecated, use `dist_backend` instead.')

        super().__init__(topk, thrs, dist_backend=dist_backend, **kwargs)

    def process(self, data_batch, data_samples: Sequence[dict]):
        """Process one batch of data samples.

        Parse predictions and labels from ``data_samples`` and invoke
        ``self.add``.

        Args:
            data_batch: A batch of data from the dataloader.
            data_samples (Sequence[dict]): A batch of outputs from the model.
        """
        predictions = []
        labels = []

        for data_sample in data_samples:
            pred_label = data_sample['pred_label']
            if 'score' in pred_label:
                predictions.append(pred_label['score'].cpu())
            else:
                predictions.append(pred_label['label'].reshape(-1).cpu())
            labels.append(data_sample['gt_label']['label'].cpu())

        if 'score' in data_samples[0]['pred_label']:
            predictions = torch.stack(predictions)
            labels = torch.stack(labels)
        else:
            predictions = torch.cat(predictions)
            labels = torch.cat(labels)

        self.add(predictions, labels)

    def evaluate(self, *args, **kwargs) -> Dict:
        """Returns metric results and reset state.

        This method would be invoked by ``mmengine.Evaluator``.
        """
        metric_results = self.compute(*args, **kwargs)
        metric_results = {
            f'{self.name}/{k}': 100 * v
            for k, v in metric_results.items()
        }
        self.reset()
        return metric_results

    def calculate(
        self,
        preds: Union[torch.Tensor, np.ndarray, Sequence],
        targets: Union[torch.Tensor, np.ndarray, Sequence],
    ) -> Union[torch.Tensor, List[List[torch.Tensor]]]:
        """Calculate the accuracy.

        We usually use this ``calculate`` method during the training satge.

        Args:
            preds (torch.Tensor | np.ndarray | Sequence): The prediction
                results. It can be labels (N, ), or scores of every
                class (N, C).
            targets (torch.Tensor | np.ndarray | Sequence): The target of
                each prediction with shape (N, ).

        Returns:
            torch.Tensor | List[List[torch.Tensor]]: Accuracy.
            - torch.Tensor: If the ``pred`` is a sequence of label instead of
              score (number of dimensions is 1). Only return a top-1 accuracy
              tensor, and ignore the argument ``topk` and ``thrs``.
            - List[List[torch.Tensor]]: If the ``pred`` is a sequence of score
              (number of dimensions is 2). Return the accuracy on each ``topk``
              and ``thrs``. And the first dim is ``topk``, the second dim is
              ``thrs``.
        """
        preds_tensor = to_tensor(preds)
        targets_tensor = to_tensor(targets).to(torch.int64)
        preds = [pred for pred in preds_tensor]
        targets = [target for target in targets_tensor]
        metric_results = self._compute_metric(preds, targets)
        metric_results = [[val * 100. for val in vals]
                          for vals in metric_results]
        if preds_tensor.ndim == 1:
            return metric_results[0][0]
        else:
            return metric_results


@METRICS.register_module()
class SingleLabelMetric(BaseMetric):
    """A collection of metrics for single-label multi-class classification task
    based on confusion matrix.

    It includes precision, recall, f1-score and support. Comparing with
    :class:`Accuracy`, these metrics doesn't support topk, but supports
    various average mode.

    Args:
        thrs (Sequence[float | None] | float | None): Predictions with scores
            under the thresholds are considered negative. None means no
            thresholds. Defaults to 0.
        items (Sequence[str]): The detailed metric items to evaluate. Here is
            the available options:

                - `"precision"`: The ratio tp / (tp + fp) where tp is the
                  number of true positives and fp the number of false
                  positives.
                - `"recall"`: The ratio tp / (tp + fn) where tp is the number
                  of true positives and fn the number of false negatives.
                - `"f1-score"`: The f1-score is the harmonic mean of the
                  precision and recall.
                - `"support"`: The total number of occurrences of each category
                  in the target.

            Defaults to ('precision', 'recall', 'f1-score').
        average (str, optional): The average method. If None, the scores
            for each class are returned. And it supports two average modes:

                - `"macro"`: Calculate metrics for each category, and calculate
                  the mean value over all categories.
                - `"micro"`: Calculate metrics globally by counting the total
                  true positives, false negatives and false positives.

            Defaults to "macro".
        collect_device (str): Device name used for collecting results from
            different ranks during distributed training. Must be 'cpu' or
            'gpu'. Defaults to 'cpu'.
        prefix (str, optional): The prefix that will be added in the metric
            names to disambiguate homonymous metrics of different evaluators.
            If prefix is not provided in the argument, self.default_prefix
            will be used instead. Defaults to None.

    Examples:
        >>> import torch
        >>> from mmcls.evaluation import SingleLabelMetric
        >>> # -------------------- The Basic Usage --------------------
        >>> y_pred = [0, 1, 1, 3]
        >>> y_true = [0, 2, 1, 3]
        >>> # Output precision, recall, f1-score and support.
        >>> SingleLabelMetric.calculate(y_pred, y_true, num_classes=4)
        (tensor(62.5000), tensor(75.), tensor(66.6667), tensor(4))
        >>> # Calculate with different thresholds.
        >>> y_score = torch.rand((1000, 10))
        >>> y_true = torch.zeros((1000, ))
        >>> SingleLabelMetric.calculate(y_score, y_true, thrs=(0., 0.9))
        [(tensor(10.), tensor(0.9500), tensor(1.7352), tensor(1000)),
         (tensor(10.), tensor(0.5500), tensor(1.0427), tensor(1000))]
        >>>
        >>> # ------------------- Use with Evalutor -------------------
        >>> from mmcls.structures import ClsDataSample
        >>> from mmengine.evaluator import Evaluator
        >>> data_samples = [
        ...     ClsDataSample().set_gt_label(i%5).set_pred_score(torch.rand(5))
        ...     for i in range(1000)
        ... ]
        >>> evaluator = Evaluator(metrics=SingleLabelMetric())
        >>> evaluator.process(data_samples)
        >>> evaluator.evaluate(1000)
        {'single-label/precision': 19.650691986083984,
         'single-label/recall': 19.600000381469727,
         'single-label/f1-score': 19.619548797607422}
        >>> # Evaluate on each class
        >>> evaluator = Evaluator(metrics=SingleLabelMetric(average=None))
        >>> evaluator.process(data_samples)
        >>> evaluator.evaluate(1000)
        {
            'single-label/precision_classwise': [21.1, 18.7, 17.8, 19.4, 16.1],
            'single-label/recall_classwise': [18.5, 18.5, 17.0, 20.0, 18.0],
            'single-label/f1-score_classwise': [19.7, 18.6, 17.1, 19.7, 17.0]
        }
    """
    default_prefix: Optional[str] = 'single-label'

    def __init__(self,
                 thrs: Union[float, Sequence[Union[float, None]], None] = 0.,
                 items: Sequence[str] = ('precision', 'recall', 'f1-score'),
                 average: Optional[str] = 'macro',
                 collect_device: str = 'cpu',
                 prefix: Optional[str] = None) -> None:
        super().__init__(collect_device=collect_device, prefix=prefix)

        if isinstance(thrs, float) or thrs is None:
            self.thrs = (thrs, )
        else:
            self.thrs = tuple(thrs)

        for item in items:
            assert item in ['precision', 'recall', 'f1-score', 'support'], \
                f'The metric {item} is not supported by `SingleLabelMetric`,' \
                ' please specicy from "precision", "recall", "f1-score" and ' \
                '"support".'
        self.items = tuple(items)
        self.average = average

    def process(self, data_batch, data_samples: Sequence[dict]):
        """Process one batch of data samples.

        The processed results should be stored in ``self.results``, which will
        be used to computed the metrics when all batches have been processed.

        Args:
            data_batch: A batch of data from the dataloader.
            data_samples (Sequence[dict]): A batch of outputs from the model.
        """

        for data_sample in data_samples:
            result = dict()
            pred_label = data_sample['pred_label']
            gt_label = data_sample['gt_label']
            if 'score' in pred_label:
                result['pred_score'] = pred_label['score'].cpu()
            elif ('num_classes' in pred_label):
                result['pred_label'] = pred_label['label'].cpu()
                result['num_classes'] = pred_label['num_classes']
            else:
                raise ValueError('The `pred_label` in data_samples do not '
                                 'have neither `score` nor `num_classes`.')
            result['gt_label'] = gt_label['label'].cpu()
            # Save the result to `self.results`.
            self.results.append(result)

    def compute_metrics(self, results: List):
        """Compute the metrics from processed results.

        Args:
            results (list): The processed results of each batch.

        Returns:
            Dict: The computed metrics. The keys are the names of the metrics,
            and the values are corresponding results.
        """
        # NOTICE: don't access `self.results` from the method. `self.results`
        # are a list of results from multiple batch, while the input `results`
        # are the collected results.
        metrics = {}

        def pack_results(precision, recall, f1_score, support):
            single_metrics = {}
            if 'precision' in self.items:
                single_metrics['precision'] = precision
            if 'recall' in self.items:
                single_metrics['recall'] = recall
            if 'f1-score' in self.items:
                single_metrics['f1-score'] = f1_score
            if 'support' in self.items:
                single_metrics['support'] = support
            return single_metrics

        # concat
        target = torch.cat([res['gt_label'] for res in results])
        if 'pred_score' in results[0]:
            pred = torch.stack([res['pred_score'] for res in results])
            metrics_list = self.calculate(
                pred, target, thrs=self.thrs, average=self.average)

            multi_thrs = len(self.thrs) > 1
            for i, thr in enumerate(self.thrs):
                if multi_thrs:
                    suffix = '_no-thr' if thr is None else f'_thr-{thr:.2f}'
                else:
                    suffix = ''

                for k, v in pack_results(*metrics_list[i]).items():
                    metrics[k + suffix] = v
        else:
            # If only label in the `pred_label`.
            pred = torch.cat([res['pred_label'] for res in results])
            res = self.calculate(
                pred,
                target,
                average=self.average,
                num_classes=results[0]['num_classes'])
            metrics = pack_results(*res)

        result_metrics = dict()
        for k, v in metrics.items():

            if self.average is None:
                result_metrics[k + '_classwise'] = v.cpu().detach().tolist()
            elif self.average == 'micro':
                result_metrics[k + f'_{self.average}'] = v.item()
            else:
                result_metrics[k] = v.item()

        return result_metrics

    @staticmethod
    def calculate(
        pred: Union[torch.Tensor, np.ndarray, Sequence],
        target: Union[torch.Tensor, np.ndarray, Sequence],
        thrs: Sequence[Union[float, None]] = (0., ),
        average: Optional[str] = 'macro',
        num_classes: Optional[int] = None,
    ) -> Union[torch.Tensor, List[torch.Tensor]]:
        """Calculate the precision, recall, f1-score and support.

        Args:
            pred (torch.Tensor | np.ndarray | Sequence): The prediction
                results. It can be labels (N, ), or scores of every
                class (N, C).
            target (torch.Tensor | np.ndarray | Sequence): The target of
                each prediction with shape (N, ).
            thrs (Sequence[float | None]): Predictions with scores under
                the thresholds are considered negative. It's only used
                when ``pred`` is scores. None means no thresholds.
                Defaults to (0., ).
            average (str, optional): The average method. If None, the scores
                for each class are returned. And it supports two average modes:

                    - `"macro"`: Calculate metrics for each category, and
                      calculate the mean value over all categories.
                    - `"micro"`: Calculate metrics globally by counting the
                      total true positives, false negatives and false
                      positives.

                Defaults to "macro".
            num_classes (Optional, int): The number of classes. If the ``pred``
                is label instead of scores, this argument is required.
                Defaults to None.

        Returns:
            Tuple: The tuple contains precision, recall and f1-score.
            And the type of each item is:

            - torch.Tensor: If the ``pred`` is a sequence of label instead of
              score (number of dimensions is 1). Only returns a tensor for
              each metric. The shape is (1, ) if ``classwise`` is False, and
              (C, ) if ``classwise`` is True.
            - List[torch.Tensor]: If the ``pred`` is a sequence of score
              (number of dimensions is 2). Return the metrics on each ``thrs``.
              The shape of tensor is (1, ) if ``classwise`` is False, and (C, )
              if ``classwise`` is True.
        """
        average_options = ['micro', 'macro', None]
        assert average in average_options, 'Invalid `average` argument, ' \
            f'please specicy from {average_options}.'

        pred = to_tensor(pred)
        target = to_tensor(target).to(torch.int64)
        assert pred.size(0) == target.size(0), \
            f"The size of pred ({pred.size(0)}) doesn't match "\
            f'the target ({target.size(0)}).'

        if pred.ndim == 1:
            assert num_classes is not None, \
                'Please specicy the `num_classes` if the `pred` is labels ' \
                'intead of scores.'
            gt_positive = F.one_hot(target.flatten(), num_classes)
            pred_positive = F.one_hot(pred.to(torch.int64), num_classes)
            return _precision_recall_f1_support(pred_positive, gt_positive,
                                                average)
        else:
            # For pred score, calculate on all thresholds.
            num_classes = pred.size(1)
            pred_score, pred_label = torch.topk(pred, k=1)
            pred_score = pred_score.flatten()
            pred_label = pred_label.flatten()

            gt_positive = F.one_hot(target.flatten(), num_classes)

            results = []
            for thr in thrs:
                pred_positive = F.one_hot(pred_label, num_classes)
                if thr is not None:
                    pred_positive[pred_score <= thr] = 0
                results.append(
                    _precision_recall_f1_support(pred_positive, gt_positive,
                                                 average))

            return results
