from typing import Union
import torch
import logging
from os.path import exists
import numpy as np


class JetNet(torch.utils.data.Dataset):
    """
    PyTorch `torch.utils.data.Dataset` class for the JetNet dataset, shape is [num_jets, num_particles, num_features].
    Features, in order: if polar coords - [eta, phi, pt, mask], if cartesian coords - [px, py, pz, mask].

    Dataset is downloaded from https://zenodo.org/record/4834876/ if pt or csv file is not found in the `data_dir` directory.

    Args:
        jet_type (str): 'g' (gluon), 't' (top quarks), or 'q' (light quarks).
        data_dir (str): directory which contains (or in which to download) dataset. Defaults to "./" i.e. the working directory.
        download (bool): download the dataset, even if the csv file exists already. Defaults to False.
        num_particles (int): number of particles to use, has to be less than the total in JetNet (30). 0 means use all. Defaults to 0.
        feature_norms (list[float]): max absolute value of each feature (in order) when normalizing. None means feature won't be scaled. Defaults to [1.0, 1.0, 1., 1.].
        feature_shifts (list[float]): shifts features by this value *after* scaling to maxes in `feature_norms`. None or 0 means won't be shifted. Defaults to [0., 0., -0.5, -0.5].
        use_mask (bool): Defaults to True.
        train (bool): whether for training or testing. Defaults to True.
        train_fraction (float): fraction of data to use as training - rest is for testing. Defaults to 0.7.
        num_pad_particles (int): how many out of `num_particles` should be zero-padded. Defaults to 0.
        use_num_particles_jet_feature (bool): Store the # of particles in each jet as a jet-level feature. Only works if using mask. Defaults to True.
        noise_padding (bool): instead of 0s, pad extra particles with Gaussian noise. Only works if using mask. Defaults to False.
    """

    def __init__(
        self,
        jet_type: str,
        data_dir: str = "./",
        download: bool = False,
        num_particles: int = 0,
        feature_norms: list[float] = [1.0, 1.0, 1.0, 1.0],
        feature_shifts: list[float] = [0.0, 0.0, -0.5, -0.5],
        use_mask: bool = True,
        train: bool = True,
        train_fraction: float = 0.7,
        num_pad_particles: int = 0,
        use_num_particles_jet_feature: bool = True,
        noise_padding: bool = False
    ):
        self.num_particles = num_particles
        self.feature_norms = feature_norms
        self.feature_shifts = feature_shifts
        self.use_mask = use_mask
        # in the future there'll be more jet features such as jet pT and eta
        self.use_jet_features = (use_num_particles_jet_feature) and self.use_mask
        self.noise_padding = noise_padding and self.use_masks

        pt_file = f"{data_dir}/{jet_type}_jets.pt"

        if not exists(pt_file) or download:
            self.download_and_convert_to_pt(data_dir, jet_type)

        logging.info("Loading dataset")
        dataset = self.load_dataset(pt_file, num_particles, num_pad_particles, use_mask)

        if self.use_jet_features:
            jet_features = self.get_jet_features(dataset, use_num_particles_jet_feature)

        logging.info(f"Loaded dataset {dataset.shape = } \n Normalizing features")
        dataset, self.feature_maxes, self.pt_cutoff = self.normalize_features(dataset, feature_norms, feature_shifts)

        if self.noise_padding:
            dataset = self.add_noise_padding(dataset)

        tcut = int(len(dataset) * train_fraction)

        self.data = dataset[:tcut] if train else dataset[tcut:]
        if self.use_jet_features:
            self.jet_features = jet_features[:tcut] if train else jet_features[tcut:]

        logging.info("Dataset processed")


    def download_and_convert_to_pt(self, data_dir: str, jet_type: str):
        """Download jet dataset and convert and save to pytorch tensor"""
        csv_file = f"{data_dir}/{jet_type}_jets.csv"

        if not exists(csv_file):
            logging.info(f"Downloading {jet_type} jets csv")
            self.download(jet_type, csv_file)

        logging.info(f"Converting {jet_type} jets csv to pt")
        self.csv_to_pt(data_dir, jet_type, csv_file)

    def download(self, jet_type: str, csv_file: str):
        """Downloads the `jet_type` jet csv from Zenodo and saves it as `csv_file`"""
        import requests
        import sys

        records_url = "https://zenodo.org/api/records/5502543"
        r = requests.get(records_url).json()
        key = f"{jet_type}_jets.csv"
        file_url = next(item for item in r['files'] if item["key"] == key)['links']['self']  # finding the url for the particular jet type dataset
        logging.info(f"{file_url = }")

        # modified from https://sumit-ghosh.com/articles/python-download-progress-bar/
        with open(csv_file, "wb") as f:
            response = requests.get(file_url, stream=True)
            total = response.headers.get("content-length")

            if total is None:
                f.write(response.content)
            else:
                downloaded = 0
                total = int(total)

                for data in response.iter_content(chunk_size=max(int(total / 1000), 1024 * 1024)):
                    downloaded += len(data)
                    f.write(data)
                    done = int(50 * downloaded / total)
                    sys.stdout.write("\r[{}{}] {:.0f}%".format("█" * done, "." * (50 - done), float(downloaded / total) * 100))
                    sys.stdout.flush()

        sys.stdout.write("\n")

    def csv_to_pt(self, data_dir: str, jet_type: str, csv_file: str):
        """Converts and saves downloaded csv file to pytorch tensor"""
        import numpy as np

        pt_file = f"{data_dir}/{jet_type}_jets.pt"
        torch.save(torch.tensor(np.loadtxt(csv_file).reshape(-1, 30, 4)), pt_file)


    def load_dataset(self, pt_file: str, num_particles: int, num_pad_particles: int, use_mask: bool):
        """Load the dataset"""
        dataset = torch.load(pt_file).float()

        # only retain up to `num_particles`, subtracting `num_pad_particles` since they will be padded below
        if 0 < num_particles - num_pad_particles < dataset.shape[1]:
            dataset = dataset[:, : num_particles - num_pad_particles, :]

        # pad with `num_pad_particles` particles
        if num_pad_particles > 0:
            dataset = torch.nn.functional.pad(dataset, (0, 0, 0, num_pad_particles), "constant", 0)

        if not use_mask:
            dataset = dataset[:, :, :-1]  # remove mask feature from dataset if not needed

        return dataset


    def get_jet_features(self, dataset: torch.Tensor, use_num_particles_jet_feature: bool):
        """Returns jet-level features. Will be expanded to jet pT and eta"""
        jet_num_particles = (torch.sum(dataset[:, :, 3], dim=1) / self.num_particles).unsqueeze(1)
        logging.debug("{num_particles = }")
        return jet_num_particles


    def normalize_features(self, dataset: torch.Tensor, feature_norms: list[float], feature_shifts: list[float]):
        """
        Normalizes dataset features, by scaling to `feature_norms` maximum and shifting by `feature_shifts`.
        If the value in the list for a feature is None, it won't be scaled or shifted.
        """
        num_features = dataset.shape[2]

        feature_maxes = [float(torch.max(torch.abs(dataset[:, :, i]))) for i in range(num_features)]
        logging.debug(f"{feature_maxes = }")

        for i in range(num_features):
            if feature_norms[i] is not None:
                dataset[:, :, i] /= feature_maxes[i]
                dataset[:, :, i] *= feature_norms[i]

            if feature_shifts[i] is not None and feature_shifts[i] != 0:
                dataset[:, :, i] += feature_shifts[i]

        pt_cutoff = torch.unique(dataset[:, :, 2], sorted=True)[1]  # smallest particle pT after 0, for the cutoff masking strategy
        logging.debug(f"{pt_cutoff = }")

        return dataset, feature_maxes, pt_cutoff

    def unnormalize_features(
        self,
        dataset: Union[torch.Tensor, np.array],
        ret_mask_separate: bool = True,
        is_real_data: bool = False,
        zero_mask_particles: bool = True,
        zero_neg_pt: bool = True,
    ):
        """
        Inverts the `normalize_features()` function on the input `dataset` array or tensor, plus optionally zero's the masked particles and negative pTs.

        Args:
            dataset (Union[torch.Tensor, np.array]): Dataset to unnormalize.
            ret_mask_separate (bool): Return the jet and mask separately. Defaults to True.
            is_real_data (bool): Real or generated data. Defaults to False.
            zero_mask_particles (bool): Set features of zero-masked particles to 0. Not needed for real data. Defaults to True.
            zero_neg_pt (bool): Set pt to 0 for particles with negative pt. Not needed for real data. Defaults to True.

        Returns:
            Unnormalized dataset of same type as input
        """
        num_features = dataset.shape[2]

        for i in range(num_features):
            if self.feature_shifts[i] is not None and self.feature_shifts[i] != 0:
                dataset[:, :, i] -= self.feature_shifts[i]

            if self.feature_norms[i] is not None:
                dataset[:, :, i] /= self.feature_norms[i]
                dataset[:, :, i] *= self.feature_maxes[i]

        mask = dataset[:, :, -1] >= 0.5 if self.use_mask else None

        if not is_real_data and zero_mask_particles and self.use_mask:
            dataset[~mask] = 0

        if not is_real_data and zero_neg_pt:
            dataset[:, :, 2][dataset[:, :, 2] < 0] = 0

        return dataset[:, :, -1], mask if ret_mask_separate else dataset


    def add_noise_padding(self, dataset: torch.Tensor):
        """ Add Gaussian noise to zero-masked particles """
        logging.debug(f"Pre-noise padded dataset: \n {dataset[:2, -10:]}")

        noise_padding = torch.randn((len(dataset), self.num_particles, dataset.shape[2] - 1)) / 5  # up to 5 sigmas will be within ±1
        noise_padding[noise_padding > 1] = 1
        noise_padding[noise_padding < -1] = -1
        noise_padding[:, :, 2] /= 2.  # pt is scaled between ±0.5

        mask = (dataset[:, :, 3] + 0.5).bool()
        noise_padding[mask] = 0  # only adding noise to zero-masked particles
        dataset += torch.cat((noise_padding, torch.zeros((len(dataset), self.num_particles, 1))), dim=2)

        logging.debug("Post-noise padded dataset: \n {dataset[:2, -10:]}")

        return dataset


    def __len__(self):
        return len(self.X)

    def __getitem__(self, idx):
        return self.data[idx], self.jet_features[idx] if self.use_jet_features else self.data[idx]