#!/usr/bin/env python
"""
  XAFS pre-edge subtraction, normalization algorithms
"""

import sys
import numpy as np
from scipy import polyfit

from larch.larchlib import plugin_path

# put the 'std' and 'xafs' (this!) plugin directories into sys.path
sys.path.insert(0, plugin_path('std'))
sys.path.insert(0, plugin_path('xafs'))

# now we can reliably import other std and xafs modules...
from mathutils import index_of, index_nearest, remove_dups
from xafsutils import set_xafsGroup

MODNAME = '_xafs'
MAX_NNORM = 5

def find_e0(energy, mu, group=None, _larch=None):
    """calculate E0 given mu(energy)

    This finds the point with maximum derivative with some
    checks to avoid spurious glitches.

    Arguments
    ----------
    energy:  array of x-ray energies, in eV
    mu:      array of mu(E)
    group:   output group

    Returns
    -------
     value for e0


    In addition, group.e0 will be set to value for e0


    """
    if _larch is None:
        raise Warning("cannot find e0 -- larch broken?")

    energy = remove_dups(energy)
    dmu = np.diff(mu)/np.diff(energy)
    # find points of high derivative
    high_deriv_pts = np.where(dmu >  max(dmu)*0.05)[0]
    idmu_max, dmu_max = 0, 0
    for i in high_deriv_pts:
        if (dmu[i] > dmu_max and
            (i+1 in high_deriv_pts) and
            (i-1 in high_deriv_pts)):
            idmu_max, dmu_max = i, dmu[i]

    e0 = energy[idmu_max+1]
    group = set_xafsGroup(group, _larch=_larch)
    group.e0 = e0
    return e0

def pre_edge(energy, mu, group=None, e0=None, step=None,
             nnorm=3, nvict=0, pre1=None, pre2=-50,
             norm1=100, norm2=None, _larch=None):
    """pre edge subtraction, normalization for XAFS

    This performs a number of steps:
       1. determine E0 (if not supplied) from max of deriv(mu)
       2. fit a line of polymonial to the region below the edge
       3. fit a polymonial to the region above the edge
       4. extrapolae the two curves to E0 to determine the edge jump

    Arguments
    ----------
    energy:  array of x-ray energies, in eV
    mu:      array of mu(E)
    group:   output group
    e0:      edge energy, in eV.  If None, it will be determined here.
    step:    edge jump.  If None, it will be determined here.
    pre1:    low E range (relative to E0) for pre-edge fit
    pre2:    high E range (relative to E0) for pre-edge fit
    nvict:   energy exponent to use for pre-edg fit.  See Note
    norm1:   low E range (relative to E0) for post-edge fit
    norm2:   high E range (relative to E0) for post-edge fit
    nnorm:   number of terms in polynomial (that is, 1+degree) for
             post-edge, normalization curve. Default=3 (quadratic), max=5

    Returns
    -------
      None

    The following attributes will be written to the output group:
        e0          energy origin
        edge_step   edge step
        norm        normalized mu(E)
        pre_edge    determined pre-edge curve
        post_edge   determined post-edge, normalization curve

    (if the output group is None, _sys.xafsGroup will be written to)

    Notes
    -----
       nvict gives an exponent to the energy term for the pre-edge fit.
       That is, a line (m * energy + b) is fit to mu(energy)*energy**nvict
       over the pr-edge regin, energy=[e0+pre1, e0+pre2].
    """

    if _larch is None:
        raise Warning("cannot remove pre_edge -- larch broken?")
    if e0 is None or e0 < energy[0] or e0 > energy[-1]:
        e0 = find_e0(energy, mu, group=group, _larch=_larch)

    energy = remove_dups(energy)
    nnorm = max(min(nnorm, MAX_NNORM), 1)
    ie0 = index_nearest(energy, e0)
    e0 = energy[ie0]

    if pre1 is None:  pre1  = min(energy) - e0
    if norm2 is None: norm2 = max(energy) - e0

    p1 = index_of(energy, pre1+e0)
    p2 = index_nearest(energy, pre2+e0)
    if p2-p1 < 2:
        p2 = min(len(energy), p1 + 2)

    omu  = mu*energy**nvict
    precoefs = polyfit(energy[p1:p2], omu[p1:p2], 1)
    pre_edge = (precoefs[0] * energy + precoefs[1]) * energy**(-nvict)
    # normalization
    p1 = index_of(energy, norm1+e0)
    p2 = index_nearest(energy, norm2+e0)
    if p2-p1 < 2:
        p2 = min(len(energy), p1 + 2)
    coefs = polyfit(energy[p1:p2], omu[p1:p2], nnorm)
    post_edge = 0
    norm_coefs = []
    for n, c in enumerate(reversed(list(coefs))):
        post_edge += c * energy**(n-nvict)
        norm_coefs.append(c)
    edge_step = post_edge[ie0] - pre_edge[ie0]
    norm  = (mu - pre_edge)/edge_step

    group = set_xafsGroup(group, _larch=_larch)
    group.e0 = e0
    group.norm = norm
    group.nvict = nvict
    group.nnorm = nnorm
    group.edge_step  = edge_step
    group.pre_edge   = pre_edge
    group.post_edge  = post_edge
    group.pre_slope  = precoefs[0]
    group.pre_offset = precoefs[1]
    for i in range(MAX_NNORM):
        if hasattr(group, 'norm_c%i' % i):
            delattr(group, 'norm_c%i' % i)
    for i, c in enumerate(norm_coefs):
        setattr(group, 'norm_c%i' % i, c)
    return

def registerLarchPlugin():
    return (MODNAME, {'find_e0': find_e0,
                      'pre_edge': pre_edge})