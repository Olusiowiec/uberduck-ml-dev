# AUTOGENERATED! DO NOT EDIT! File to edit: nbs/data_loader.ipynb (unless otherwise specified).

__all__ = ['pad_sequences', 'prepare_input_sequence', 'oversample', 'TextMelDataset', 'TextMelCollate',
           'TextAudioSpeakerLoader', 'TextAudioSpeakerCollate', 'DistributedBucketSampler']

# Cell
import os
import random
import re
from pathlib import Path
from typing import List

import numpy as np
from scipy.io.wavfile import read
import torch
from torch.utils.data import Dataset
from torch.utils.data.distributed import DistributedSampler

from .models.common import STFT, MelSTFT
from .text.symbols import (
    DEFAULT_SYMBOLS,
    IPA_SYMBOLS,
    NVIDIA_TACO2_SYMBOLS,
    GRAD_TTS_SYMBOLS,
)
from .text.util import cleaned_text_to_sequence, text_to_sequence
from .utils.audio import compute_yin, load_wav_to_torch
from .utils.utils import (
    load_filepaths_and_text,
    intersperse,
)

# Cell
def pad_sequences(batch):
    input_lengths = torch.LongTensor([len(x) for x in batch])
    max_input_len = input_lengths.max()

    text_padded = torch.LongTensor(len(batch), max_input_len)
    text_padded.zero_()
    for i in range(len(batch)):
        text = batch[i]
        text_padded[i, : text.size(0)] = text

    return text_padded, input_lengths


def prepare_input_sequence(
    texts, cpu_run=False, arpabet=False, symbol_set=NVIDIA_TACO2_SYMBOLS
):
    p_arpabet = float(arpabet)
    seqs = []
    for text in texts:
        seqs.append(
            torch.IntTensor(
                text_to_sequence(
                    text,
                    ["english_cleaners"],
                    p_arpabet=p_arpabet,
                    symbol_set=symbol_set,
                )[:]
            )
        )
    text_padded, input_lengths = pad_sequences(seqs)
    if not cpu_run:
        text_padded = text_padded.cuda().long()
        input_lengths = input_lengths.cuda().long()
    else:
        text_padded = text_padded.long()
        input_lengths = input_lengths.long()

    return text_padded, input_lengths

# Cell
from collections import defaultdict


def oversample(filepaths_text_sid, sid_to_weight):
    assert all([isinstance(sid, str) for sid in sid_to_weight.keys()])
    output = []
    for fts in filepaths_text_sid:
        sid = fts[2]
        for _ in range(sid_to_weight.get(sid, 1)):
            output.append(fts)
    return output

# Cell


def _orig_to_dense_speaker_id(speaker_ids):

    id_order = np.argsort(np.asarray(speaker_ids, dtype=int))
    output = {
        orig: idx for orig, idx in zip(speaker_ids[id_order], range(len(speaker_ids)))
    }
    return output


class TextMelDataset(Dataset):
    def __init__(
        self,
        audiopaths_and_text: str,
        text_cleaners: List[str],
        p_arpabet: float,
        n_mel_channels: int,
        sampling_rate: int,
        mel_fmin: float,
        mel_fmax: float,
        filter_length: int,
        hop_length: int,
        win_length: int,
        symbol_set: str,
        padding: int = None,
        max_wav_value: float = 32768.0,
        include_f0: bool = False,
        pos_weight: float = 10,
        f0_min: int = 80,
        f0_max: int = 880,
        harmonic_thresh=0.25,
        debug: bool = False,
        debug_dataset_size: int = None,
        oversample_weights=None,
        intersperse_text: bool = False,
        intersperse_token: int = 0,
        compute_gst=None,
    ):
        super().__init__()
        path = audiopaths_and_text
        oversample_weights = oversample_weights or {}
        self.audiopaths_and_text = oversample(
            load_filepaths_and_text(path), oversample_weights
        )
        self.text_cleaners = text_cleaners
        self.p_arpabet = p_arpabet

        self.stft = MelSTFT(
            filter_length=filter_length,
            hop_length=hop_length,
            win_length=win_length,
            n_mel_channels=n_mel_channels,
            sampling_rate=sampling_rate,
            mel_fmin=mel_fmin,
            mel_fmax=mel_fmax,
            padding=padding,
        )
        self.max_wav_value = max_wav_value
        self.sampling_rate = sampling_rate
        self.filter_length = filter_length
        self.hop_length = hop_length
        self.mel_fmin = mel_fmin
        self.mel_fmax = mel_fmax
        self.include_f0 = include_f0
        self.f0_min = f0_min
        self.f0_max = f0_max
        self.harmonic_threshold = harmonic_thresh
        # speaker id lookup table
        speaker_ids = [i[2] for i in self.audiopaths_and_text]
        self._speaker_id_map = _orig_to_dense_speaker_id(speaker_ids)
        self.debug = debug
        self.debug_dataset_size = debug_dataset_size
        self.symbol_set = symbol_set
        self.intersperse_text = intersperse_text
        self.intersperse_token = intersperse_token
        self.compute_gst = compute_gst

    def _get_f0(self, audio):
        f0, harmonic_rates, argmins, times = compute_yin(
            audio,
            self.sampling_rate,
            self.filter_length,
            self.hop_length,
            self.f0_min,
            self.f0_max,
            self.harmonic_threshold,
        )
        pad = int((self.filter_length / self.hop_length) / 2)
        f0 = [0.0] * pad + f0 + [0.0] * pad
        f0 = np.array(f0, dtype=np.float32)
        return f0

    def _get_gst(self, transcription):
        return self.compute_gst(transcription)

    def _get_data(self, audiopath_and_text):
        path, transcription, speaker_id = audiopath_and_text
        speaker_id = self._speaker_id_map[speaker_id]
        sampling_rate, wav_data = read(path)
        text_sequence = torch.LongTensor(
            text_to_sequence(
                transcription,
                self.text_cleaners,
                p_arpabet=self.p_arpabet,
                symbol_set=self.symbol_set,
            )
        )
        if self.intersperse_text:
            text_sequence = torch.LongTensor(
                intersperse(text_sequence.numpy(), self.intersperse_token)
            )  # add a blank token, whose id number is len(symbols)

        audio = torch.FloatTensor(wav_data)
        audio_norm = audio / self.max_wav_value
        audio_norm = audio_norm.unsqueeze(0)
        melspec = self.stft.mel_spectrogram(audio_norm)
        melspec = torch.squeeze(melspec, 0)
        data = {
            "text_sequence": text_sequence,
            "mel": melspec,
            "speaker_id": speaker_id,
            "embedded_gst": None,
            "f0": None,
        }

        if self.compute_gst:
            embedded_gst = self._get_gst([transcription])
            data["embedded_gst"] = embedded_gst

        if self.include_f0:
            f0 = self._get_f0(audio.data.cpu().numpy())
            f0 = torch.from_numpy(f0)[None]
            f0 = f0[:, : melspec.size(1)]
            data["f0"] = f0

        return data  # (text_sequence, melspec, speaker_id, f0)

    def __getitem__(self, idx):
        """Return data for a single audio file + transcription."""
        try:
            data = self._get_data(self.audiopaths_and_text[idx])
        except Exception as e:
            print(f"Error while getting data: {self.audiopaths_and_text[idx]}")
            print(e)
            raise
        return data

    def __len__(self):
        if self.debug and self.debug_dataset_size:
            return min(self.debug_dataset_size, len(self.audiopaths_and_text))
        return len(self.audiopaths_and_text)

    def sample_test_batch(self, size):
        idx = np.random.choice(range(len(self)), size=size, replace=False)
        test_batch = []
        for index in idx:
            test_batch.append(self.__getitem__(index))
        return test_batch

# Cell


class TextMelCollate:
    def __init__(self, n_frames_per_step: int = 1, include_f0: bool = False):
        self.n_frames_per_step = n_frames_per_step
        self.include_f0 = include_f0

    def set_frames_per_step(self, n_frames_per_step):
        """Set n_frames_step.

        This is used to train with gradual training, where we start with a large
        n_frames_per_step in order to learn attention quickly and decrease it
        over the course of training in order to increase accuracy. Gradual training
        reference:
        https://erogol.com/gradual-training-with-tacotron-for-faster-convergence/
        """
        self.n_frames_per_step = n_frames_per_step

    def __call__(self, batch):
        """Collate's training batch from normalized text and mel-spectrogram
        PARAMS
        ------
        batch: [text_normalized, mel_normalized, speaker_id]
        """
        # Right zero-pad all one-hot text sequences to max input length
        input_lengths, ids_sorted_decreasing = torch.sort(
            torch.LongTensor([len(x["text_sequence"]) for x in batch]),
            dim=0,
            descending=True,
        )
        max_input_len = input_lengths[0]

        text_padded = torch.LongTensor(len(batch), max_input_len)
        text_padded.zero_()
        for i in range(len(ids_sorted_decreasing)):
            text = batch[ids_sorted_decreasing[i]]["text_sequence"]
            text_padded[i, : text.size(0)] = text

        # Right zero-pad mel-spec
        num_mels = batch[0]["mel"].size(0)
        max_target_len = max([x["mel"].size(1) for x in batch])
        if max_target_len % self.n_frames_per_step != 0:
            max_target_len += (
                self.n_frames_per_step - max_target_len % self.n_frames_per_step
            )
            assert max_target_len % self.n_frames_per_step == 0

        # include mel padded, gate padded and speaker ids
        mel_padded = torch.FloatTensor(len(batch), num_mels, max_target_len)
        mel_padded.zero_()
        gate_padded = torch.FloatTensor(len(batch), max_target_len)
        gate_padded.zero_()
        output_lengths = torch.LongTensor(len(batch))
        speaker_ids = torch.LongTensor(len(batch))
        if self.include_f0:
            f0_padded = torch.FloatTensor(len(batch), 1, max_target_len)
            f0_padded.zero_()

        # pdb.set_trace()
        for i in range(len(ids_sorted_decreasing)):
            mel = batch[ids_sorted_decreasing[i]]["mel"]
            mel_padded[i, :, : mel.size(1)] = mel
            gate_padded[i, mel.size(1) - 1 :] = 1
            output_lengths[i] = mel.size(1)
            speaker_ids[i] = batch[ids_sorted_decreasing[i]]["speaker_id"]

        if batch[0]["embedded_gst"] is None:
            embedded_gsts = None
        else:
            embedded_gsts = torch.FloatTensor(
                np.array([sample["embedded_gst"] for sample in batch])
            )

        model_inputs = (
            text_padded,
            input_lengths,
            mel_padded,
            gate_padded,
            output_lengths,
            speaker_ids,
            embedded_gsts,
        )
        return model_inputs

# Cell


class TextAudioSpeakerLoader(Dataset):
    """
    1) loads audio, speaker_id, text pairs
    2) normalizes text and converts them to sequences of integers
    3) computes spectrograms from audio files.
    """

    def __init__(
        self, audiopaths_sid_text, hparams, debug=False, debug_dataset_size=None
    ):
        oversample_weights = hparams.oversample_weights or {}
        self.audiopaths_sid_text = oversample(
            load_filepaths_and_text(audiopaths_sid_text), oversample_weights
        )
        self.text_cleaners = hparams.text_cleaners
        self.max_wav_value = hparams.max_wav_value
        self.sampling_rate = hparams.sampling_rate
        self.filter_length = hparams.filter_length
        self.hop_length = hparams.hop_length
        self.win_length = hparams.win_length
        self.sampling_rate = hparams.sampling_rate

        self.debug = debug
        self.debug_dataset_size = debug_dataset_size

        self.stft = MelSTFT(
            filter_length=self.filter_length,
            hop_length=self.hop_length,
            win_length=self.win_length,
            n_mel_channels=hparams.n_mel_channels,
            sampling_rate=hparams.sampling_rate,
            mel_fmin=hparams.mel_fmin,
            mel_fmax=hparams.mel_fmax,
            padding=(self.filter_length - self.hop_length) // 2,
        )

        self.cleaned_text = getattr(hparams, "cleaned_text", False)
        # NOTE(zach): Parametrize this later if desired.
        self.symbol_set = IPA_SYMBOLS

        self.add_blank = hparams.add_blank
        self.min_text_len = getattr(hparams, "min_text_len", 1)
        self.max_text_len = getattr(hparams, "max_text_len", 190)

        random.seed(1234)
        random.shuffle(self.audiopaths_sid_text)
        self._filter()

    def _filter(self):
        """
        Filter text & store spec lengths
        """
        # Store spectrogram lengths for Bucketing
        # wav_length ~= file_size / (wav_channels * Bytes per dim) = file_size / (1 * 2)
        # spec_length = wav_length // hop_length

        audiopaths_sid_text_new = []
        lengths = []
        for audiopath, sid, text in self.audiopaths_sid_text:
            if self.min_text_len <= len(text) and len(text) <= self.max_text_len:
                audiopaths_sid_text_new.append([audiopath, sid, text])
                lengths.append(os.path.getsize(audiopath) // (2 * self.hop_length))
        self.audiopaths_sid_text = audiopaths_sid_text_new
        self.lengths = lengths

    def get_audio_text_speaker_pair(self, audiopath_sid_text):
        # separate filename, speaker_id and text
        audiopath, text, sid = (
            audiopath_sid_text[0],
            audiopath_sid_text[1],
            audiopath_sid_text[2],
        )
        text = self.get_text(text)
        spec, wav = self.get_audio(audiopath)
        sid = self.get_sid(sid)
        return (text, spec, wav, sid)

    def get_audio(self, filename):
        audio, sampling_rate = load_wav_to_torch(filename)
        if sampling_rate != self.sampling_rate:
            raise ValueError(
                "{} {} SR doesn't match target {} SR".format(
                    sampling_rate, self.sampling_rate
                )
            )

        audio_norm = audio / self.max_wav_value
        audio_norm = audio_norm.unsqueeze(0)
        spec_filename = filename.replace(".wav", ".uberduck.spec.pt")
        if os.path.exists(spec_filename):
            spec = torch.load(spec_filename)
        else:
            spec = self.stft.spectrogram(audio_norm)
            spec = torch.squeeze(spec, 0)
            torch.save(spec, spec_filename)
        return spec, audio_norm

    def get_text(self, text):
        if self.cleaned_text:
            text_norm = cleaned_text_to_sequence(text, symbol_set=self.symbol_set)
        else:
            text_norm = text_to_sequence(
                text, self.text_cleaners, symbol_set=self.symbol_set
            )
        if self.add_blank:
            text_norm = intersperse(text_norm, 0)
        text_norm = torch.LongTensor(text_norm)
        return text_norm

    def get_sid(self, sid):
        sid = torch.LongTensor([int(sid)])
        return sid

    def __getitem__(self, index):
        return self.get_audio_text_speaker_pair(self.audiopaths_sid_text[index])

    def __len__(self):
        if self.debug and self.debug_dataset_size:
            return min(self.debug_dataset_size, len(self.audiopaths_sid_text))
        else:
            return len(self.audiopaths_sid_text)

# Cell


class TextAudioSpeakerCollate:
    """Zero-pads model inputs and targets"""

    def __init__(self, return_ids=False):
        self.return_ids = return_ids

    def __call__(self, batch):
        """Collate's training batch from normalized text, audio and speaker identities
        PARAMS
        ------
        batch: [text_normalized, spec_normalized, wav_normalized, sid]
        """
        # Right zero-pad all one-hot text sequences to max input length
        _, ids_sorted_decreasing = torch.sort(
            torch.LongTensor([x[1].size(1) for x in batch]), dim=0, descending=True
        )

        max_text_len = max([len(x[0]) for x in batch])
        max_spec_len = max([x[1].size(1) for x in batch])
        max_wav_len = max([x[2].size(1) for x in batch])

        text_lengths = torch.LongTensor(len(batch))
        spec_lengths = torch.LongTensor(len(batch))
        wav_lengths = torch.LongTensor(len(batch))
        sid = torch.LongTensor(len(batch))

        text_padded = torch.LongTensor(len(batch), max_text_len)
        spec_padded = torch.FloatTensor(len(batch), batch[0][1].size(0), max_spec_len)
        wav_padded = torch.FloatTensor(len(batch), 1, max_wav_len)
        text_padded.zero_()
        spec_padded.zero_()
        wav_padded.zero_()
        for i in range(len(ids_sorted_decreasing)):
            row = batch[ids_sorted_decreasing[i]]

            text = row[0]
            text_padded[i, : text.size(0)] = text
            text_lengths[i] = text.size(0)

            spec = row[1]
            spec_padded[i, :, : spec.size(1)] = spec
            spec_lengths[i] = spec.size(1)

            wav = row[2]
            wav_padded[i, :, : wav.size(1)] = wav
            wav_lengths[i] = wav.size(1)

            sid[i] = row[3]

        if self.return_ids:
            return (
                text_padded,
                text_lengths,
                spec_padded,
                spec_lengths,
                wav_padded,
                wav_lengths,
                sid,
                ids_sorted_decreasing,
            )
        return (
            text_padded,
            text_lengths,
            spec_padded,
            spec_lengths,
            wav_padded,
            wav_lengths,
            sid,
        )

# Cell


class DistributedBucketSampler(DistributedSampler):
    """
    Maintain similar input lengths in a batch.
    Length groups are specified by boundaries.
    Ex) boundaries = [b1, b2, b3] -> any batch is included either {x | b1 < length(x) <=b2} or {x | b2 < length(x) <= b3}.

    It removes samples which are not included in the boundaries.
    Ex) boundaries = [b1, b2, b3] -> any x s.t. length(x) <= b1 or length(x) > b3 are discarded.
    """

    def __init__(
        self,
        dataset,
        batch_size,
        boundaries,
        num_replicas=None,
        rank=None,
        shuffle=True,
    ):
        super().__init__(dataset, num_replicas=num_replicas, rank=rank, shuffle=shuffle)
        self.lengths = dataset.lengths
        self.batch_size = batch_size
        self.boundaries = boundaries

        self.buckets, self.num_samples_per_bucket = self._create_buckets()
        self.total_size = sum(self.num_samples_per_bucket)
        self.num_samples = self.total_size // self.num_replicas

    def _create_buckets(self):
        buckets = [[] for _ in range(len(self.boundaries) - 1)]
        for i in range(len(self.lengths)):
            length = self.lengths[i]
            idx_bucket = self._bisect(length)
            if idx_bucket != -1:
                buckets[idx_bucket].append(i)

        for i in range(len(buckets) - 1, 0, -1):
            if len(buckets[i]) == 0:
                buckets.pop(i)
                self.boundaries.pop(i + 1)

        num_samples_per_bucket = []
        for i in range(len(buckets)):
            len_bucket = len(buckets[i])
            total_batch_size = self.num_replicas * self.batch_size
            rem = (
                total_batch_size - (len_bucket % total_batch_size)
            ) % total_batch_size
            num_samples_per_bucket.append(len_bucket + rem)
        return buckets, num_samples_per_bucket

    def __iter__(self):
        # deterministically shuffle based on epoch
        g = torch.Generator()
        g.manual_seed(self.epoch)

        indices = []
        if self.shuffle:
            for bucket in self.buckets:
                indices.append(torch.randperm(len(bucket), generator=g).tolist())
        else:
            for bucket in self.buckets:
                indices.append(list(range(len(bucket))))

        batches = []
        for i in range(len(self.buckets)):
            bucket = self.buckets[i]
            len_bucket = len(bucket)
            ids_bucket = indices[i]
            num_samples_bucket = self.num_samples_per_bucket[i]

            # add extra samples to make it evenly divisible
            rem = num_samples_bucket - len_bucket
            ids_bucket = (
                ids_bucket
                + ids_bucket * (rem // len_bucket)
                + ids_bucket[: (rem % len_bucket)]
            )

            # subsample
            ids_bucket = ids_bucket[self.rank :: self.num_replicas]

            # batching
            for j in range(len(ids_bucket) // self.batch_size):
                batch = [
                    bucket[idx]
                    for idx in ids_bucket[
                        j * self.batch_size : (j + 1) * self.batch_size
                    ]
                ]
                batches.append(batch)

        if self.shuffle:
            batch_ids = torch.randperm(len(batches), generator=g).tolist()
            batches = [batches[i] for i in batch_ids]
        self.batches = batches

        assert len(self.batches) * self.batch_size == self.num_samples
        return iter(self.batches)

    def _bisect(self, x, lo=0, hi=None):
        if hi is None:
            hi = len(self.boundaries) - 1

        if hi > lo:
            mid = (hi + lo) // 2
            if self.boundaries[mid] < x and x <= self.boundaries[mid + 1]:
                return mid
            elif x <= self.boundaries[mid]:
                return self._bisect(x, lo, mid)
            else:
                return self._bisect(x, mid + 1, hi)
        else:
            return -1

    def __len__(self):
        return self.num_samples // self.batch_size