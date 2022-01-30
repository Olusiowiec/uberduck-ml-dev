# AUTOGENERATED! DO NOT EDIT! File to edit: nbs/monitoring.generate.ipynb (unless otherwise specified).

__all__ = ['MODEL_TYPES']

# Cell
from ..data_loader import prepare_input_sequence
from ..data.batch import Batch

MODEL_TYPES = ["OD", "OD", "OD", "OD", "OD", "D"]


def _get_inference(model, vocoder, texts, speaker_ids, symbol_set, arpabet):

    text_padded, input_lengths = prepare_input_sequence(
        texts, cpu_run=False, arpabet=arpabet, symbol_set=symbol_set
    )
    input_ = Batch(
        text_int_padded=text_padded,
        input_lengths=input_lengths,
        speaker_ids=speaker_ids,
    )
    output = model.inference(input_)
    audio = vocoder.infer(output[1][:1])
    return audio