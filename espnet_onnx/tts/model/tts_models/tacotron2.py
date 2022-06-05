from typing import (
    Optional,
    Tuple,
    List,
    Dict,
)

import onnxruntime
import numpy as np

from espnet_onnx.asr.frontend.frontend import Frontend
from espnet_onnx.asr.frontend.global_mvn import GlobalMVN
from espnet_onnx.asr.frontend.utterance_mvn import UtteranceMVN
from espnet_onnx.asr.scorer.interface import BatchScorerInterface
from espnet_onnx.utils.function import make_pad_mask
from espnet_onnx.utils.config import Config


class Tacotron2:
    def __init__(
        self,
        config: Config,
        providers: List[str],
        use_quantized: bool = False,
    ):
        self.config = config
        # load encoder and decoder.
        self.encoder = self._load_model(config.encoder, providers, use_quantized)
        self.predecoder = self._load_model(config.decoder.predecoder,
                                providers, use_quantized)
        self.decoder = self._load_model(config.decoder, providers, use_quantized)
        
        if self.config.decoder.postdecoder.onnx_export:
            self.postdecoder = self._load_model(config.decoder.postdecoder,
                                providers, use_quantized)
        else:
            self.postdecoder = None
        
        # HP
        self.input_names = [d.name for d in self.encoder.get_inputs()]
        self.use_sids = 'sids' in self.input_names
        self.use_lids = 'lids' in self.input_names
        self.use_feats = 'feats' in self.input_names
        self.dlayers = self.config.decoder.dlayers
        self.dunits = self.config.decoder.dunits
        self.decoder_input_names = [d.name for d in self.decoder.get_inputs()]
        self.decoder_output_names = [d.name for d in self.decoder.get_outputs()]
    
    def _load_model(self, config, providers, use_quantized):
        if use_quantized:
            return onnxruntime.InferenceSession(
                config.quantized_model_path,
                providers=providers
            )
        else:
            return onnxruntime.InferenceSession(
                config.model_path,
                providers=providers
            )

    def __call__(
        self,
        text: np.ndarray,
        feats: np.ndarray = None,
        sids: np.ndarray = None,
        spembs:  np.ndarray = None,
        lids:  np.ndarray = None
    ):
        # compute encoder and initialize states
        input_enc = self.get_input_enc(text, feats, sids, spembs, lids)
        h = self.encoder.run(['h'], input_enc)[0]
        
        idx = 0
        outs = []
        probs = []
        att_ws = []
        maxlen = int(len(text) * self.config.decoder.maxlenratio)
        minlen = int(len(text) * self.config.decoder.minlenratio)
        c_list, z_list, a_prev, prev_out = self.init_state(h)
        
        # compute decoder
        while True:
            idx += self.config.decoder.reduction_factor
            input_dec = self.get_input_dec(c_list, z_list, a_prev, prev_out)
            out, prob, a_prev, prev_out, *cz_states = \
                self.decoder.run(self.decoder_output_names, input_dec)
            c_list, z_list = self._split(cz_states)
            
            outs += [out]
            probs += [prob]
            att_ws += [a_prev]
            
            # check whether to finish generation
            if (
                int(sum(prob >= self.config.decoder.threshold)) > 0
                or idx >= maxlen
            ):
                # check mininum length
                if idx < minlen:
                    continue
                outs = np.concatenate(outs, axis=2)  # (1, odim, L)
                if self.postdecoder is not None:
                    outs = self.postdecoder.run(['out'], {
                        'x': outs
                    })[0] # (1, odim, L)
                    
                probs = np.concatenate(probs, axis=0)
                att_ws = np.concatenate(att_ws, axis=0)
                break
        
        return dict(feat_gen=outs, prob=probs, att_w=att_ws)

    def get_input_enc(self, text, feats, sids, spembs, lids):
        ret = {'text': text }
        ret = self._set_input_dict(ret, 'feats', feats)
        ret = self._set_input_dict(ret, 'sids', sids)
        ret = self._set_input_dict(ret, 'spembs', spembs)
        ret = self._set_input_dict(ret, 'lids', lids)
        return ret

    def get_input_dec(self, c_list, z_list, a_prev, prev_in):
        ret = {}
        ret.update({
            f'c_prev_{i}': cl
            for i, cl in enumerate(c_list)
        })
        ret.update({
            f'z_prev_{i}': zl
            for i, zl in enumerate(z_list)
        })
        ret.update({
            'a_prev': a_prev,
            'pceh': self.pre_compute_enc_h,
            'enc_h': self.enc_h,
            'mask': self.mask,
            'prev_in': prev_in
        })
        return ret

    def _set_input_dict(self, dic, key, value):
        if key in self.input_names:
            assert value is not None
            dic[key] = value
        return dic
    
    def zero_state(self, hs_pad):
        return np.zeros((hs_pad.shape[0], self.dunits), dtype=np.float32)

    def get_att_prev(self, x, att_type=None):
        att_prev = 1.0 - make_pad_mask([x.shape[0]])
        att_prev = (
            att_prev / np.array([x.shape[0]])[..., None]).astype(np.float32)
        if att_type == 'location2d':
            att_prev = att_prev[..., None].reshape(-1, self.config.att_win, -1)
        if att_type in ('coverage', 'coverage_location'):
            att_prev = att_prev[:, None, :]
        return att_prev
    
    def init_state(self, x):
        # to support mutiple encoder asr mode, in single encoder mode,
        # convert torch.Tensor to List of torch.Tensor
        c_list = [self.zero_state(x[None, :])]
        z_list = [self.zero_state(x[None, :])]
        for _ in range(1, self.dlayers):
            c_list.append(self.zero_state(x[None, :]))
            z_list.append(self.zero_state(x[None, :]))

        a = self.get_att_prev(x)
        prev_out = np.zeros((1, self.config.decoder.odim), dtype=np.float32)
        
        # compute predecoder
        self.pre_compute_enc_h = self.predecoder.run(
            ['pre_compute_enc_h'], { 'enc_h': x[None, :] }
        )[0]
        self.enc_h = x[None, :]
        self.mask = np.where(make_pad_mask(
                [x.shape[0]]) == 1, -10000.0, 0).astype(np.float32)
        
        return c_list[:], z_list[:], a, prev_out

    def _split(self, status_lists):
        len_list = len(status_lists)
        c_list = status_lists[ : len_list // 2]
        z_list = status_lists[len_list // 2 : ]
        return c_list, z_list
