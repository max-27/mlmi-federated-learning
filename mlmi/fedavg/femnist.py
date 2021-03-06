from typing import Optional, Tuple
from torch.utils import data
from torch.utils.data.dataset import T_co
from torchvision.datasets import MNIST

from mlmi.structs import FederatedDatasetData

import numpy as np
from torchvision import datasets, transforms

from itertools import permutations


class DatasetSplit(data.Dataset):
    def __init__(self, dataset, idxs):
        self.dataset = dataset
        self.idxs = list(idxs)

    def __len__(self):
        return len(self.idxs)

    def __getitem__(self, item):
        image, label = self.dataset[self.idxs[item]]
        return image, label


def load_mnist_dataset(data_dir, num_clients=100, batch_size=10):
    """
    Sample non-I.I.D client data from MNIST dataset
    :param dataset:
    :param num_users:
    :return:
    """
    trans_mnist = transforms.Compose([transforms.ToTensor()])
    dataset_train: MNIST = datasets.MNIST(data_dir, train=True, download=True, transform=trans_mnist)
    dataset_test: MNIST = datasets.MNIST(data_dir, train=False, download=True, transform=trans_mnist)

    num_shards, num_imgs = 200, 300
    idx_shard = [i for i in range(num_shards)]
    partitions = {i: np.array([], dtype='int64') for i in range(num_clients)}
    idxs = np.arange(num_shards * num_imgs)

    labels = dataset_train.targets.numpy()

    # sort labels
    idxs_labels = np.vstack((idxs, labels))
    idxs_labels = idxs_labels[:, idxs_labels[1, :].argsort()]
    idxs = idxs_labels[0, :]

    # divide and assign
    for i in range(num_clients):
        rand_set = set(np.random.choice(idx_shard, 2, replace=False))
        idx_shard = list(set(idx_shard) - rand_set)
        for rand in rand_set:
            partitions[i] = np.concatenate((partitions[i], idxs[rand * num_imgs:(rand + 1) * num_imgs]), axis=0)
    test_idxs = np.split(np.arange(len(dataset_test.targets)), num_shards)

    data_local_num_dict = dict()
    data_local_test_num_dict = dict()
    data_local_train_num_dict = dict()
    train_data_local_dict = dict()
    test_data_local_dict = dict()
    for i, partition in enumerate(partitions):
        train_data_local_dict[i] = data.DataLoader(
            DatasetSplit(dataset_train, partitions[i]), batch_size=batch_size, shuffle=True)
        test_data_local_dict[i] = data.DataLoader(
            DatasetSplit(dataset_test, test_idxs[i]), batch_size=batch_size, shuffle=True)
        data_local_test_num_dict[i] = len(test_data_local_dict[i])
        data_local_train_num_dict[i] = len(train_data_local_dict[i])
        data_local_num_dict[i] = data_local_test_num_dict[i] + data_local_train_num_dict[i]

    train_data_global = data.DataLoader(dataset=dataset_test, batch_size=batch_size, shuffle=True, drop_last=False)
    test_data_global = data.DataLoader(dataset=dataset_train, batch_size=batch_size, shuffle=True, drop_last=False)

    result_dataset = FederatedDatasetData(client_num=num_clients,
                                          train_data_global=train_data_global,
                                          test_data_global=test_data_global,
                                          data_local_num_dict=data_local_num_dict,
                                          data_local_test_num_dict=data_local_test_num_dict,
                                          data_local_train_num_dict=data_local_train_num_dict,
                                          class_num=10,
                                          train_data_local_dict=train_data_local_dict,
                                          test_data_local_dict=test_data_local_dict,
                                          name='mnist', batch_size=batch_size)
    return result_dataset


class FEMNISTDataset(data.Dataset):

    def __init__(self, labels, pixels):
        self.labels = labels
        self.pixels = pixels

    def __len__(self):
        return len(self.labels)

    def __getitem__(self, index) -> T_co:
        return self.pixels[index], self.labels[index]


def load_femnist_dataset(data_dir, num_clients=367, batch_size=10, only_digits=False, sample_threshold=-1, random_seed=0):
    import torch
    import os, collections
    os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
    import tensorflow as tf
    import tensorflow_federated as tff
    from tensorflow_federated.python.simulation import HDF5ClientData
    RANDOM = np.random.RandomState(seed=random_seed)

    _datasets: Tuple[HDF5ClientData, HDF5ClientData] = tff.simulation.datasets.emnist.load_data(only_digits=only_digits,
                                                                                                cache_dir=data_dir)
    emnist_train, emnist_test = _datasets
    selected_client_ids = RANDOM.choice(emnist_train.client_ids, size=num_clients, replace=False)
    train_data_local_dict = dict()
    data_local_num_dict = dict()
    data_local_train_num_dict = dict()
    test_data_local_dict = dict()
    data_local_test_num_dict = dict()
    if sample_threshold != -1:
        clients_exceeding_threshold = []
        for client_id in emnist_train.client_ids:
            h5data_train = collections.OrderedDict((name, ds[()]) for name, ds in sorted(
                emnist_train._h5_file[HDF5ClientData._EXAMPLES_GROUP][client_id].items()))
            if sample_threshold < len(h5data_train['label']):
                clients_exceeding_threshold.append(client_id)
        if len(clients_exceeding_threshold) < num_clients:
            raise ValueError(f'Only {len(clients_exceeding_threshold)} clients with more than {sample_threshold} '
                             f'samples available. But asked for {num_clients}.')
        selected_client_ids = RANDOM.choice(clients_exceeding_threshold, size=num_clients, replace=False)

    for client_id in selected_client_ids:
        h5data_train = collections.OrderedDict((name, ds[()]) for name, ds in sorted(
            emnist_train._h5_file[HDF5ClientData._EXAMPLES_GROUP][client_id].items()))
        train_channel_data = torch.unsqueeze(torch.from_numpy(h5data_train['pixels']), 1)
        femnist_train = FEMNISTDataset(torch.from_numpy(h5data_train['label']), train_channel_data)
        h5data_test = collections.OrderedDict((name, ds[()]) for name, ds in sorted(
            emnist_test._h5_file[HDF5ClientData._EXAMPLES_GROUP][client_id].items()))
        test_channel_data = torch.unsqueeze(torch.from_numpy(h5data_test['pixels']), 1)
        femnist_test = FEMNISTDataset(torch.from_numpy(h5data_test['label']), test_channel_data)
        dl_train = data.DataLoader(femnist_train, batch_size=batch_size)
        dl_test = data.DataLoader(femnist_test, batch_size=batch_size)
        train_data_local_dict[client_id] = dl_train
        data_local_train_num_dict[client_id] = len(femnist_train)
        data_local_train_num_dict[client_id] = len(femnist_train)
        test_data_local_dict[client_id] = dl_test
        data_local_test_num_dict[client_id] = len(femnist_test)
        data_local_num_dict[client_id] = len(femnist_train)
    train_data_num = sum([num for num in data_local_train_num_dict.values()])
    test_data_num = sum([num for num in data_local_test_num_dict.values()])
    result_dataset = FederatedDatasetData(client_num=num_clients, train_data_num=train_data_num, test_data_num=test_data_num,
                                          train_data_global=dict(),
                                          test_data_global=dict(),
                                          data_local_num_dict=data_local_num_dict,
                                          data_local_test_num_dict=data_local_test_num_dict,
                                          data_local_train_num_dict=data_local_train_num_dict,
                                          class_num=10 if only_digits else 62,
                                          train_data_local_dict=train_data_local_dict,
                                          test_data_local_dict=test_data_local_dict,
                                          name=f'femnist{num_clients}', batch_size=batch_size)
    return result_dataset


def load_femnist_colored_dataset(data_dir, num_clients=367, batch_size=10, only_digits=False,
                                 sample_threshold=-1, color_probabilities=(0.34, 0.33, 0.33), add_pattern=False):
    import torch
    import os, collections
    os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
    import tensorflow as tf
    import tensorflow_federated as tff
    from tensorflow_federated.python.simulation import HDF5ClientData

    _datasets: Tuple[HDF5ClientData, HDF5ClientData] = tff.simulation.datasets.emnist.load_data(only_digits=only_digits,
                                                                                                cache_dir=data_dir)
    emnist_train, emnist_test = _datasets
    selected_client_ids = np.random.choice(emnist_train.client_ids, size=num_clients, replace=False)
    train_data_local_dict = dict()
    data_local_num_dict = dict()
    data_local_train_num_dict = dict()
    test_data_local_dict = dict()
    data_local_test_num_dict = dict()
    if sample_threshold != -1:
        clients_exceeding_threshold = []
        for client_id in emnist_train.client_ids:
            h5data_train = collections.OrderedDict((name, ds[()]) for name, ds in sorted(
                emnist_train._h5_file[HDF5ClientData._EXAMPLES_GROUP][client_id].items()))
            if sample_threshold < len(h5data_train['label']):
                clients_exceeding_threshold.append(client_id)
        if len(clients_exceeding_threshold) < num_clients:
            raise ValueError(f'Only {len(clients_exceeding_threshold)} clients with more than {sample_threshold} '
                             f'samples available. But asked for {num_clients}.')
        selected_client_ids = np.random.choice(clients_exceeding_threshold, size=num_clients, replace=False)

    color_variants = [
        [[255, 255, 255], [0, 0, 0]],
        [[255, 0, 255], [0, 255, 0]],
        [[120, 0, 0], [0, 120, 255]]
    ]
    color_choices = np.random.choice(np.arange(len(color_variants)), size=len(selected_client_ids), replace=True,
                                     p=color_probabilities)
    patterns = np.random.choice(np.arange(len(color_variants)), size=len(selected_client_ids), replace=True,
                                p=color_probabilities)

    for color_choice, pattern, client_id in zip(color_choices, patterns, selected_client_ids):
        color_variant = color_variants[color_choice]
        if not add_pattern:
            pattern = None

        h5data_train = collections.OrderedDict((name, ds[()]) for name, ds in sorted(
            emnist_train._h5_file[HDF5ClientData._EXAMPLES_GROUP][client_id].items()))
        train_channel_data = torch.from_numpy(
            _get_colored_pixels(h5data_train['pixels'], color_variant, pattern=pattern)).type(torch.FloatTensor)
        femnist_train = FEMNISTDataset(torch.from_numpy(h5data_train['label']), train_channel_data)

        h5data_test = collections.OrderedDict((name, ds[()]) for name, ds in sorted(
            emnist_test._h5_file[HDF5ClientData._EXAMPLES_GROUP][client_id].items()))
        test_channel_data = torch.from_numpy(
            _get_colored_pixels(h5data_test['pixels'], color_variant, pattern=pattern)).type(torch.FloatTensor)
        femnist_test = FEMNISTDataset(torch.from_numpy(h5data_test['label']), test_channel_data)

        dl_train = data.DataLoader(femnist_train, batch_size=batch_size)
        dl_test = data.DataLoader(femnist_test, batch_size=batch_size)

        train_data_local_dict[client_id] = dl_train
        data_local_train_num_dict[client_id] = len(femnist_train)
        test_data_local_dict[client_id] = dl_test
        data_local_test_num_dict[client_id] = len(femnist_test)
        data_local_num_dict[client_id] = len(femnist_train)

    train_data_num = sum([num for num in data_local_train_num_dict.values()])
    test_data_num = sum([num for num in data_local_test_num_dict.values()])
    result_dataset = FederatedDatasetData(client_num=num_clients, train_data_num=train_data_num, test_data_num=test_data_num,
                                          train_data_global=dict(),
                                          test_data_global=dict(),
                                          data_local_num_dict=data_local_num_dict,
                                          data_local_test_num_dict=data_local_test_num_dict,
                                          data_local_train_num_dict=data_local_train_num_dict,
                                          class_num=10 if only_digits else 62,
                                          train_data_local_dict=train_data_local_dict,
                                          test_data_local_dict=test_data_local_dict,
                                          name=f'femnist{num_clients}', batch_size=batch_size)
    return result_dataset


def _pattern_map():
    return {
        0: _add_dots,
        1: _add_stripes,
        2: _add_circles
    }


def _get_colored_pixels(bw_images: np.ndarray, color_variant: np.ndarray, pattern: Optional[int] = None):
    colored_images = []
    white_replacement, black_replacement = color_variant[0], color_variant[1]
    for bw_image in bw_images:
        colored_image = np.zeros((3, *bw_images.shape[1:]))
        if pattern is not None:
            bw_image = _pattern_map().get(pattern)(bw_image)
        for channel in range(3):
            v1 = black_replacement[channel]
            v2 = white_replacement[channel]
            base = np.full((*bw_images.shape[1:],), v1, dtype=np.float32)
            step = bw_image * (v2 - v1)
            colored_image[channel] = base + step
        colored_images.append(colored_image)
    np_colored_images = np.stack(colored_images)
    return np_colored_images / 255


def _add_stripes(original_pixels):
    dotmap = np.ones((28, 28))
    for r in range(4, 28, 6):
        rnd = np.random.random_sample((len(dotmap[r]),))
        for c in range(len(dotmap[r])):
            dotmap[r, c] = rnd[c] * 0.4
    return original_pixels * dotmap


def _add_dots(original_pixels):
    dotmap = np.ones((28, 28))
    for r in range(4, 28, 8):
        for c in range(4, 28, 8):
            rnd = np.random.random_sample((8,))
            dotmap[r, c] = rnd[0] * 0.5
            dotmap[r - 1, c] = rnd[1] * 0.5
            dotmap[r + 1, c] = rnd[2] * 0.5
            dotmap[r, c - 1] = rnd[3] * 0.5
            dotmap[r, c + 1] = rnd[4] * 0.5
    return original_pixels * dotmap


def _add_circles(original_pixels):
    dotmap = np.ones((28, 28))
    for r in range(8, 23, 10):
        for c in range(8, 23, 10):
            rnd = np.random.random_sample((8,))
            dotmap[r - 2, c] = rnd[0] * 0.3
            dotmap[r - 1, c - 1] = rnd[1] * 0.3
            dotmap[r - 1, c + 1] = rnd[2] * 0.3
            dotmap[r + 2, c] = rnd[3] * 0.3
            dotmap[r + 1, c - 1] = rnd[4] * 0.3
            dotmap[r + 1, c + 1] = rnd[5] * 0.3
            dotmap[r, c + 2] = rnd[6] * 0.3
            dotmap[r, c - 2] = rnd[7] * 0.3
    return original_pixels * dotmap


if __name__ == '__main__':
    from mlmi.settings import REPO_ROOT
    from mlmi.utils import create_tensorboard_logger
    experiment_logger = create_tensorboard_logger('colortest', 'femnist')
    dataset = load_femnist_colored_dataset(str((REPO_ROOT / 'data').absolute()))
    dataloaders = list(dataset.train_data_local_dict.values())[0:5]
    images = []
    for dl in dataloaders:
        for i, (x, y) in enumerate(dl):
            for s in x:
                images.append(s)
    images_array = np.stack(images)
    experiment_logger.experiment.add_image('test', images_array, dataformats='NCHW')
