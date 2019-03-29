# -*- mode: python; coding: utf-8 -*
# Copyright (c) 2018 Radio Astronomy Software Group
# Licensed under the 3-clause BSD License

from __future__ import absolute_import, division, print_function

import numpy as np
import os

from pyuvdata import UVBeam
from pyuvdata.data import DATA_PATH

# functions used by many tests


def compare_dictionaries(dic1, dic2):
    """
        Recursively compare two dictionaries.
    """
    compare = True
    for k in dic1.keys():
        if isinstance(dic1[k], dict):
            compare *= compare_dictionaries(dic1[k], dic2[k])
        else:
            if isinstance(dic1[k], float):
                compare *= np.isclose(dic1[k], dic2[k], atol=1e-5)
            else:
                compare *= (dic1[k] == dic2[k])
    return bool(compare)


def make_cst_beams():
    beam = UVBeam()
    beam.freq_interp_kind = 'linear'

    cst_files = ['HERA_NicCST_150MHz.txt', 'HERA_NicCST_123MHz.txt']
    beam_files = [os.path.join(DATA_PATH, 'NicCSTbeams', f) for f in cst_files]
    beam.read_cst_beam(beam_files, beam_type='efield', frequency=[100e6, 123e6],
                       telescope_name='HERA',
                       feed_name='PAPER', feed_version='0.1', feed_pol=['x'],
                       model_name='E-field pattern - Rigging height 4.9m',
                       model_version='1.0')

    return beam
