#!/usr/bin/env python
"""
trackron training script with a torchrun script
"""

import os
import datetime
import logging
from collections import OrderedDict
import torch
import traceback
from pathlib import Path

import trackron.utils.comm as comm
from trackron.checkpoint import TrackingCheckpointer, PeriodicCheckpointer
from trackron.data import build_tracking_loader, build_tracking_test_loader
from trackron.engine import default_argument_parser, default_setup, default_writers, launch
from trackron.models import build_model
from trackron.solvers import build_optimizer_scheduler
from trackron.utils.events import EventStorage
from trackron.config import setup_cfg
import torch.distributed as dist

## for eval
from trackron.trackers import TrackingActor
from torch.nn.parallel import DistributedDataParallel as DDP
from trackron.evaluation import inference_on_dataset, SOTEvaluator, print_csv_format, MOTEvaluator, DETEvaluator, TAOEvaluator

logger = logging.getLogger("trackron")

# _TRACKING_MODE = ['sot', 'vos', 'mot']
_TRACKING_MODE = ['sot', 'mot']


def get_evaluator(dataset_name, output_dir=None, tracking_mode='sot'):
    output_dir.mkdir(parents=True, exist_ok=True)
    if tracking_mode in ['sot', 'vos']:
        evaluator = SOTEvaluator(dataset_name, output_dir=output_dir)
    elif tracking_mode == 'mot':
        kwargs = {}
        if dataset_name.startswith('tao'):
            evaluator = [
                DETEvaluator(dataset_name, output_dir=output_dir),
                TAOEvaluator(dataset_name, output_dir=output_dir)
            ]
        else:
            evaluator = [
                # DETEvaluator(dataset_name, output_dir=output_dir),
                MOTEvaluator(dataset_name, output_dir=output_dir)
            ]
    else:
        raise NotImplementedError
    return evaluator


def write_test_metric(results, storage):
    if comm.is_main_process():
        save_dicts = {}
        for data_name, result in results.items():
            for metric_name, kvs in result.items():
                for k, v in kvs.items():
                    name = f'{data_name}/{metric_name}/{k}'
                    save_dicts[name] = v

        storage.put_scalars(smoothing_hint=False, **save_dicts)


def do_test(cfg, model, tracking_mode, debug_level=-1):
    results = OrderedDict()
    tracking_actor = TrackingActor(
        cfg,
        model,
        output_dir=cfg.OUTPUT_DIR,
        tracking_mode=tracking_mode,
        tracking_category=cfg.TRACKER.TRACKING_CATEGORY,
        debug_level=debug_level)
    tracking_cfg = cfg.get(tracking_mode.upper())
    dataset_names = tracking_cfg.DATASET.TEST.DATASET_NAMES
    for idx, dataset_name in enumerate(dataset_names):
        loader = build_tracking_test_loader(tracking_cfg, dataset_name)
        version = tracking_cfg.DATASET.TEST.VERSIONS[idx]
        if version != "":
            dataset_name = f'{dataset_name}{version}'
        result_dir = Path(cfg.OUTPUT_DIR) / 'results' / dataset_name
        evaluator = get_evaluator(dataset_name,
                                  output_dir=result_dir,
                                  tracking_mode=tracking_mode)

        results_i = inference_on_dataset(tracking_actor, loader, evaluator)
        results[dataset_name] = results_i
        # if comm.is_main_process() and tracking_mode == 'sot':
        if comm.is_main_process():
            logger.info("Evaluation results for {} in csv format:".format(
                dataset_name))
            print_csv_format(results_i)
    logger.info('Testing Finished!')
    return results


def do_val(val_iters, model, iteration=20):
    model.eval()
    metrics = {}
    with torch.no_grad():
        for i in range(iteration):
            _, metric = forward_step(val_iters, model)
            for k, v in metric.items():
                metrics[k] = metrics.get(k, 0.0) + v
    metric_reduced = {
        k: v.item() / iteration
        for k, v in comm.reduce_dict(metrics).items()
    }
    # if comm.is_main_process():
    #   storage.put_scalars(**metric_reduced)
    model.train()
    return metric_reduced


def forward_step(data_iters, model, max_fail=10, mode='sot'):
    fail_time = 0
    while fail_time < max_fail:
        try:
            data = next(data_iters)
            return model(data, mode=mode)
        except Exception:
            fail_time += 1
            traceback.print_exc()
            logger.warning('retry %d th time' % fail_time)
    traceback.print_exc()
    raise ValueError('Cannot get data')


def do_train(cfg, model, resume=False, tracking_mode='sot'):
    model.train()
    optimizer, scheduler = build_optimizer_scheduler(cfg, model)

    checkpointer = TrackingCheckpointer(model,
                                        cfg.OUTPUT_DIR,
                                        optimizer=optimizer,
                                        scheduler=scheduler)
    start_iter = 0
    if resume:
        start_iter = (checkpointer.resume_or_load(
            cfg.MODEL.WEIGHTS, resume=resume).get("iteration", -1) + 1)
    else:
        ### use pretrain weight
        checkpointer.resume_or_load(cfg.MODEL.WEIGHTS, resume=resume)
    max_iter = cfg.SOLVER.MAX_ITER

    periodic_checkpointer = PeriodicCheckpointer(checkpointer,
                                                 cfg.SOLVER.CHECKPOINT_PERIOD,
                                                 max_iter=max_iter)

    writers = default_writers(cfg.OUTPUT_DIR,
                              max_iter) if comm.is_main_process() else []

    # compared to "train_net.py", we do not support accurate timing and
    # precise BN here, because they are not trivial to implement in a small training loop
    # val_loader = build_tracking_loader(cfg.SOT)
    logger.info("Starting training from iteration {}".format(start_iter))
    mode = tracking_mode
    train_iters = {}
    if tracking_mode in ['mot', 'mix']:
        train_loader = build_tracking_loader(cfg.MOT, training=True)
        train_iters['mot'] = iter(train_loader)
    if tracking_mode in ['sot', 'mix']:
        train_loader = build_tracking_loader(cfg.SOT, training=True)
        train_iters['sot'] = iter(train_loader)
    # if tracking_mode in ['vos', 'mix']:
    #   train_loader = build_tracking_loader(cfg.VOS, training=True)
    #   train_iters['vos'] = iter(train_loader)

    with EventStorage(start_iter) as storage:
        # for data, iteration in zip(data_loader, range(start_iter, max_iter)):
        for iteration in range(start_iter, max_iter):
            storage.iter = iteration
            if tracking_mode == 'mix':
                # train_iters = mot_train_iters if iteration % 2 == 0 else sot_train_iters
                # mode = 'mot' if iteration % 2 == 0 else 'sot'
                mode = _TRACKING_MODE[iteration % len(_TRACKING_MODE)]
                train_iters
            loss_dict, weighted_loss_dict, metrics = forward_step(
                train_iters[mode], model, mode=mode)

            losses = sum(weighted_loss_dict.values())
            assert torch.isfinite(losses).all(), loss_dict

            loss_dict_reduced = {
                k: v.item()
                for k, v in comm.reduce_dict(weighted_loss_dict).items()
            }
            metric_reduced = {
                k: v.item()
                for k, v in comm.reduce_dict(metrics).items()
            } if metrics is not None else {}
            losses_reduced = sum(loss for loss in loss_dict_reduced.values())
            if comm.is_main_process():
                if mode == 'sot':
                    storage.put_scalars(sot_total_loss=losses_reduced,
                                        **loss_dict_reduced)
                else:
                    storage.put_scalars(mot_total_loss=losses_reduced,
                                        **loss_dict_reduced)
                storage.put_scalars(**metric_reduced)

            optimizer.zero_grad()
            losses.backward()
            optimizer.step()
            storage.put_scalar("lr",
                               optimizer.param_groups[0]["lr"],
                               smoothing_hint=False)
            scheduler.step_update(iteration + 1)

            if (cfg.TEST.ETEST_PERIOD > 0 and
                (iteration + 1) % cfg.TEST.ETEST_PERIOD == 0
                    or iteration == max_iter - 1):
                results = do_test(cfg, model, tracking_mode=mode)
                write_test_metric(results, storage)
                # Compared to "train_net.py", the test results are not dumped to EventStorage
                comm.synchronize()

            # if iteration - start_iter > 5 and ((iteration + 1) % 20 == 0
            #                                    or iteration == max_iter - 1):
            #     for writer in writers:
            #         writer.write()
            for writer in writers:
                writer.write()
            periodic_checkpointer.step(iteration)


def main(args):
    # cfg = setup(args)
    cfg = setup_cfg(args)
    default_setup(cfg, args)

    model = build_model(cfg)
    if cfg.SYNC_BN:
        model = torch.nn.SyncBatchNorm.convert_sync_batchnorm(model)

    # TRACED 模型的结构信息太长，如果需要再进行输出即可！
    logger.info("Model:\n{}".format(model))
    distributed = comm.get_world_size() > 1
    if distributed:
        model = DDP(model,
                    device_ids=[int(os.environ['LOCAL_RANK'])],
                    broadcast_buffers=False,
                    find_unused_parameters=args.unused_parameter)

    if args.eval_only:
        TrackingCheckpointer(model, save_dir=cfg.OUTPUT_DIR).resume_or_load(
            cfg.MODEL.WEIGHTS, resume=args.resume)

        # default: sot
        tracking_mode = args.mode if args.mode != 'mix' else 'sot'
        return do_test(cfg,
                       model,
                       tracking_mode=tracking_mode,
                       debug_level=args.debug)

    do_train(cfg, model, resume=args.resume, tracking_mode=args.mode)


if __name__ == "__main__":
    parser = default_argument_parser()
    # XBL comment;
    # 这里主要涉及到模型继续训练的参数设置问题，以及 argparse 里面的 action 参数
    # args = parser.parse_args('--resume'.split())
    args = parser.parse_args()
    # TRACED 模型的结构信息太长，如果需要再进行输出即可！
    print("Command Line Args:", args)

    if os.environ.get('LOCAL_RANK', -1) != -1:
        dist.init_process_group(backend='nccl', timeout=datetime.timedelta(1))
        # BUG RuntimeError: CUDA error: invalid device ordinal
        # CUDA kernel errors might be asynchronously reported at some other API call,so the stacktrace below might be incorrect.
        torch.cuda.set_device(int(os.environ['LOCAL_RANK']))
    else:
        torch.cuda.set_device(0)
    main(args)
