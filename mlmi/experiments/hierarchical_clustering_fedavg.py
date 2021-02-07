from typing import Callable, Dict, List, Optional

from sacred import Experiment
from functools import partial

from torch import Tensor, optim

from mlmi.clustering import ModelFlattenWeightsPartitioner, AlternativePartitioner, RandomClusterPartitioner
from mlmi.experiments.log import log_goal_test_acc, log_loss_and_acc
from mlmi.fedavg.data import scratch_labels
from mlmi.fedavg.femnist import load_femnist_dataset
from mlmi.fedavg.model import CNNLightning, CNNMnistLightning, FedAvgServer
from mlmi.fedavg.run import run_fedavg
from mlmi.fedavg.structs import FedAvgExperimentContext
from mlmi.fedavg.util import load_fedavg_state, run_fedavg_round, run_fedavg_train_round
from mlmi.hierarchical.run import run_fedavg_hierarchical
from mlmi.participant import BaseTrainingParticipant
from mlmi.plot import generate_client_label_heatmap, generate_data_label_heatmap
from mlmi.settings import REPO_ROOT
from mlmi.structs import ClusterArgs, FederatedDatasetData, ModelArgs, OptimizerArgs, TrainArgs
from mlmi.utils import create_tensorboard_logger, fix_random_seeds, overwrite_participants_models

ex = Experiment('hierachical_clustering')


@ex.config
def default_configuration():
    seed = 123123123
    lr = 0.1
    name = 'default_hierarchical_fedavg'
    total_fedavg_rounds = 20
    cluster_initialization_rounds = [1, 3, 5, 10]
    client_fraction = [0.1]
    local_epochs = 3
    batch_size = 10
    num_clients = 367
    sample_threshold = -1
    num_label_limit = -1
    num_classes = 62
    optimizer_args = OptimizerArgs(optim.SGD, lr=lr)
    train_args = TrainArgs(max_epochs=local_epochs, min_epochs=local_epochs, progress_bar_refresh_rate=0)
    model_args = ModelArgs(CNNLightning, optimizer_args=optimizer_args, only_digits=False)
    dataset = 'femnist'
    partitioner_class = ModelFlattenWeightsPartitioner
    linkage_mech = 'ward'
    criterion = 'distance'
    dis_metric = 'euclidean'
    max_value_criterion = 10.0


@ex.named_config
def hpsearch():
    seed = 123123123
    lr = [0.068]
    name = 'hpsearch'
    total_fedavg_rounds = 75
    cluster_initialization_rounds = [5, 10, 15, 20]
    client_fraction = [0.1]
    local_epochs = 3
    batch_size = 10
    num_clients = 367
    sample_threshold = 250  # we need clients with at least 250 samples to make sure all labels are present
    num_label_limit = 15
    num_classes = 62
    optimizer_args = OptimizerArgs(optim.SGD, lr=lr)
    train_args = TrainArgs(max_epochs=local_epochs, min_epochs=local_epochs, progress_bar_refresh_rate=0)
    model_args = ModelArgs(CNNLightning, optimizer_args=optimizer_args, only_digits=False)
    dataset = 'femnist'
    partitioner_class = AlternativePartitioner
    linkage_mech = 'ward'
    criterion = 'distance'
    dis_metric = 'euclidean'
    max_value_criterion = [3.5, 4.0, 5.0]


@ex.named_config
def briggs():
    seed = 123123123
    lr = 0.1
    name = 'briggs'
    total_fedavg_rounds = 50
    cluster_initialization_rounds = [1, 3, 5, 10]
    client_fraction = [0.1, 0.2, 0.5]
    local_epochs = 3
    batch_size = 10
    num_clients = 367
    sample_threshold = 250  # we need clients with at least 250 samples to make sure all labels are present
    num_label_limit = 15
    num_classes = 62
    optimizer_args = OptimizerArgs(optim.SGD, lr=lr)
    train_args = TrainArgs(max_epochs=local_epochs, min_epochs=local_epochs, progress_bar_refresh_rate=0)
    model_args = ModelArgs(CNNLightning, optimizer_args=optimizer_args, only_digits=False)
    dataset = 'femnist'
    partitioner_class = ModelFlattenWeightsPartitioner
    linkage_mech = 'ward'
    criterion = 'distance'
    dis_metric = 'euclidean'
    max_value_criterion = 10.0


def log_after_round_evaluation(
        experiment_logger,
        tag: str,
        loss: Tensor,
        acc: Tensor,
        step: int
):
    log_loss_and_acc(tag, loss, acc, experiment_logger, step)
    log_goal_test_acc(tag, acc, experiment_logger, step)


def log_cluster_distribution(
        experiment_logger,
        cluster_clients_dic: Dict[str, List['BaseTrainingParticipant']],
        num_classes
):
    for cluster_id, clients in cluster_clients_dic.items():
        image = generate_client_label_heatmap(f'cluster {cluster_id}', clients, num_classes)
        experiment_logger.experiment.add_image(f'label distribution/cluster_{cluster_id}', image.numpy())


def log_dataset_distribution(experiment_logger, tag: str, dataset: FederatedDatasetData):
    dataloaders = list(dataset.train_data_local_dict.values())
    image = generate_data_label_heatmap(tag, dataloaders, dataset.class_num)
    experiment_logger.experiment.add_image('label distribution', image.numpy())


def generate_configuration(init_rounds_list, max_value_criterion_list):
    for ri in init_rounds_list:
        for mv in max_value_criterion_list:
            yield ri, mv


@ex.automain
def run_hierarchical_clustering(
        seed,
        lr,
        name,
        total_fedavg_rounds,
        cluster_initialization_rounds,
        client_fraction,
        local_epochs,
        batch_size,
        num_clients,
        sample_threshold,
        num_label_limit,
        #optimizer_args,
        train_args,
        model_args,
        dataset,
        partitioner_class,
        linkage_mech,
        criterion,
        dis_metric,
        max_value_criterion
):
    fix_random_seeds(seed)
    global_tag = 'global_performance'

    if dataset == 'femnist':
        fed_dataset = load_femnist_dataset(str((REPO_ROOT / 'data').absolute()),
                                           num_clients=num_clients, batch_size=batch_size,
                                           sample_threshold=sample_threshold)
        if num_label_limit != -1:
            fed_dataset = scratch_labels(fed_dataset, num_label_limit)
    else:
        raise ValueError(f'dataset "{dataset}" unknown')

    if not hasattr(max_value_criterion, '__iter__'):
        max_value_criterion = [max_value_criterion]


    data_distribution_logged = False
    for cf in client_fraction:
        for lr_i in lr:
            optimizer_args = OptimizerArgs(optim.SGD, lr=lr_i)
            model_args = ModelArgs(CNNLightning, optimizer_args=optimizer_args, only_digits=False)
            fedavg_context = FedAvgExperimentContext(name=name, client_fraction=cf, local_epochs=local_epochs,
                                                     lr=lr_i, batch_size=batch_size, optimizer_args=optimizer_args,
                                                     model_args=model_args, train_args=train_args,
                                                     dataset_name=dataset)
            experiment_specification = f'{fedavg_context}'
            experiment_logger = create_tensorboard_logger(fedavg_context.name, experiment_specification)
            if not data_distribution_logged:
                log_dataset_distribution(experiment_logger, 'full dataset', fed_dataset)
                data_distribution_logged = True

            log_after_round_evaluation_fns = [
                partial(log_after_round_evaluation, experiment_logger, 'fedavg'),
                partial(log_after_round_evaluation, experiment_logger, global_tag)
            ]
            server, clients = run_fedavg(context=fedavg_context, num_rounds=total_fedavg_rounds, dataset=fed_dataset,
                                         save_states=True, restore_state=True,
                                         after_round_evaluation=log_after_round_evaluation_fns)

            for init_rounds, max_value in generate_configuration(cluster_initialization_rounds, max_value_criterion):
                # load the model state
                round_model_state = load_fedavg_state(fedavg_context, init_rounds)
                overwrite_participants_models(round_model_state, clients)
                # initialize the cluster configuration
                round_configuration = {
                    'num_rounds_init': init_rounds,
                    'num_rounds_cluster': total_fedavg_rounds - init_rounds
                }
                cluster_args = ClusterArgs(partitioner_class, linkage_mech=linkage_mech,
                                           criterion=criterion, dis_metric=dis_metric,
                                           max_value_criterion=max_value,
                                           plot_dendrogram=False, **round_configuration)
                # create new logger for cluster experiment
                experiment_specification = f'{fedavg_context}_{cluster_args}'
                experiment_logger = create_tensorboard_logger(fedavg_context.name, experiment_specification)
                fedavg_context.experiment_logger = experiment_logger

                initial_train_fn = partial(run_fedavg_train_round, round_model_state, training_args=train_args)
                create_aggregator_fn = partial(FedAvgServer, model_args=model_args, context=fedavg_context)
                federated_round_fn = partial(run_fedavg_round, training_args=train_args, client_fraction=cf)

                after_post_clustering_evaluation = [
                    partial(log_after_round_evaluation, experiment_logger, 'post_clustering')
                ]
                after_clustering_round_evaluation = [
                    partial(log_after_round_evaluation, experiment_logger)
                ]
                after_federated_round_evaluation = [
                    partial(log_after_round_evaluation, experiment_logger, 'final hierarchical'),
                    partial(log_after_round_evaluation, experiment_logger, global_tag)
                ]
                after_clustering_fn = [
                    partial(log_cluster_distribution, experiment_logger, num_classes=fed_dataset.class_num)
                ]
                run_fedavg_hierarchical(server, clients, cluster_args,
                                        initial_train_fn,
                                        federated_round_fn,
                                        create_aggregator_fn,
                                        after_post_clustering_evaluation,
                                        after_clustering_round_evaluation,
                                        after_federated_round_evaluation,
                                        after_clustering_fn)
