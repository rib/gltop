#!/usr/bin/env python2

# Copyright (C) 2015-2016 Intel Corporation
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

# Generates code for metric sets supported by the drm i915-perf driver
# including:
#
# - Static arrays describing the various NOA/Boolean/OA register configs
# - Functions/structs for advertising metrics via sysfs
# - Code that can evaluate which configs are available for the current system
#   based on the RPN availability equations
#


import argparse
import copy
import hashlib
from operator import itemgetter
import re
import sys

import xml.etree.cElementTree as et

import pylibs.codegen as codegen
import pylibs.oa_guid_registry as oa_registry

default_set_blacklist = {}


def underscore(name):
    s = re.sub('MHz', 'Mhz', name)
    s = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', s)
    return re.sub('([a-z0-9])([A-Z])', r'\1_\2', s).lower()


def print_err(*args):
    sys.stderr.write(' '.join(map(str,args)) + '\n')


def brkt(subexp):
    if " " in subexp:
        return "(" + subexp + ")"
    else:
        return subexp

def splice_bitwise_and(args):
    return brkt(args[1]) + " & " + brkt(args[0])

def splice_logical_and(args):
    return brkt(args[1]) + " && " + brkt(args[0])

def splice_ult(args):
    return brkt(args[1]) + " < " + brkt(args[0])

def splice_ugte(args):
    return brkt(args[1]) + " >= " + brkt(args[0])

exp_ops = {}
#                 (n operands, splicer)
exp_ops["AND"]  = (2, splice_bitwise_and)
exp_ops["UGTE"] = (2, splice_ugte)
exp_ops["ULT"]  = (2, splice_ult)
exp_ops["&&"]   = (2, splice_logical_and)


c_syms = {}
c_syms["$SliceMask"] = "INTEL_INFO(dev_priv)->sseu.slice_mask"
c_syms["$SubsliceMask"] = "INTEL_INFO(dev_priv)->sseu.subslice_mask"
c_syms["$SkuRevisionId"] = "dev_priv->drm.pdev->revision"

mnemonic_syms = {}
mnemonic_syms["$SliceMask"] = "slices"
mnemonic_syms["$SubsliceMask"] = "subslices"
mnemonic_syms["$SkuRevisionId"] = "sku"

copyright = """/*
 * Autogenerated file by GPU Top : https://github.com/rib/gputop
 * DO NOT EDIT manually!
 *
 *
 * Copyright (c) 2015 Intel Corporation
 *
 * Permission is hereby granted, free of charge, to any person obtaining a
 * copy of this software and associated documentation files (the "Software"),
 * to deal in the Software without restriction, including without limitation
 * the rights to use, copy, modify, merge, publish, distribute, sublicense,
 * and/or sell copies of the Software, and to permit persons to whom the
 * Software is furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice (including the next
 * paragraph) shall be included in all copies or substantial portions of the
 * Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.  IN NO EVENT SHALL
 * THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
 * FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS
 * IN THE SOFTWARE.
 *
 */

"""


def output_b_counter_config(metric_set, config):
    c("\nstatic const struct i915_oa_reg b_counter_config_" + metric_set['perf_name_lc'] + "[] = {")

    c.indent(8)

    n_regs = 0
    for reg in config.findall("register"):
        assert reg.get('type') == 'OA'

        addr = int(reg.get('address'), 16)
        addr_str = "0x%x" % addr
        val = int(reg.get('value'), 16)
        val_str = "0x%08x" % val

        c("{ _MMIO(" + addr_str + "), " + val_str + " },")
        n_regs = n_regs + 1

    c.outdent(8)

    c("};")


def output_flex_config(metric_set, config):
    c("\nstatic const struct i915_oa_reg flex_eu_config_" + metric_set['perf_name_lc'] + "[] = {")

    c.indent(8)

    n_regs = 0
    for reg in config.findall("register"):
        assert reg.get('type') == 'FLEX'

        addr = int(reg.get('address'), 16)
        addr_str = "0x%x" % addr
        val = int(reg.get('value'), 16)
        val_str = "0x%08x" % val

        c("{ _MMIO(" + addr_str + "), " + val_str + " },")
        n_regs = n_regs + 1

    c.outdent(8)

    c("};")


def exp_to_symbol(exp):
    exp = exp.replace(' & ', '_')
    exp = exp.replace(' && ', '_and_')
    exp = exp.replace(' >= ', '_gte_')
    exp = exp.replace(' < ', '_lt_')
    exp = exp.replace(' ', '_')
    exp = exp.replace('(', '')
    exp = exp.replace(')', '')
    exp = exp.replace(')', '')

    return exp


def count_config_mux_registers(config):
    n_regs = 0
    for reg in config.findall("register"):
        if reg.get('type') == 'NOA':
            addr = reg.get('address')
            n_regs = n_regs + 1
    return n_regs


def output_mux_config(metric_set, config):
    c("\nstatic const struct i915_oa_reg mux_config_" + metric_set['perf_name_lc'] + "[] = {")

    c.indent(8)
    for reg in config.findall("register"):
        assert reg.get('type') == 'NOA'
        addr = int(reg.get('address'), 16)
        addr_str = "0x%x" % addr
        val = int(reg.get('value'), 16)
        val_str = "0x%08x" % val

        c("{ _MMIO(" + addr_str + "), " + val_str + " },")
    c.outdent(8)

    c("};")


def output_config(metric_set, config):
    c("\mstatic const struct i915_oa_config_" + metric_set['perf_name_lc'] + " = {")
    c.indent(8)
    c(".uuid = \"" + metric_set['guid'] + "\",");
    c(".id = 1,")
    c("\n")
    c(".mux_regs = mux_config_" + metric_set['perf_name_lc'] + "_oa,")
    c(".mux_regs_len = ARRAY_SIZE(mux_config_" + metric_set['perf_name_lc'] + "_oa),")
    c("\n")
    c(".b_counter_regs = b_counter_config_" + metric_set['perf_name_lc'] + "_oa,")
    c(".b_counter_regs_len = ARRAY_SIZE(b_counter_config_" + metric_set['perf_name_lc'] + "_oa),")
    c("\n")
    c(".flex_eu_regs = flex_eu_config_" + metric_set['perf_name_lc'] + "_oa,")
    c(".flex_eu_counter_regs_len = ARRAY_SIZE(flex_eu_config_" + metric_set['perf_name_lc'] + "_oa),")
    c("\n")
    c(".sysfs_metric = {")
    c.indent(8)
    c(".name = \"" + metric_set['guid'] + "\",")
    c.outdent(8)
    c("},")
    c.outdent(8)
    c("};")

def output_config_select(metric_set):
    c("dev_priv->perf.oa.mux_regs =")
    c.indent(8)
    c("mux_config_" + metric_set['perf_name_lc']  + ";")
    c.outdent(8)
    c("dev_priv->perf.oa.mux_regs_len =")
    c.indent(8)
    c("ARRAY_SIZE(mux_config_" + metric_set['perf_name_lc']  + ");")
    c.outdent(8)

    c("\n")
    c("dev_priv->perf.oa.b_counter_regs =")
    c.indent(8)
    c("b_counter_config_" + metric_set['perf_name_lc']  + ";")
    c.outdent(8)
    c("dev_priv->perf.oa.b_counter_regs_len =")
    c.indent(8)
    c("ARRAY_SIZE(b_counter_config_" + metric_set['perf_name_lc']  + ");")
    c.outdent(8)

    c("\n")
    c("dev_priv->perf.oa.flex_regs =")
    c.indent(8)
    c("flex_eu_config_" + metric_set['perf_name_lc']  + ";")
    c.outdent(8)
    c("dev_priv->perf.oa.flex_regs_len =")
    c.indent(8)
    c("ARRAY_SIZE(flex_eu_config_" + metric_set['perf_name_lc']  + ");")
    c.outdent(8)


def output_sysfs_code(sets):
    for metric_set in sets:
        perf_name = metric_set['perf_name']
        perf_name_lc = metric_set['perf_name_lc']

        c("\n")
        c("static ssize_t")
        c("show_" + perf_name_lc + "_id(struct device *kdev, struct device_attribute *attr, char *buf)")
        c("{")
        c.indent(8)
        c("return sprintf(buf, \"1\\n\");")
        c.outdent(8)
        c("}")


    h("extern void i915_perf_load_test_config_" + chipset.lower() + "(struct drm_i915_private *dev_priv);")
    h("\n")

    c("\n")
    c("void")
    c("i915_perf_load_test_config_" + chipset.lower() + "(struct drm_i915_private *dev_priv)")
    c("{")
    c.indent(8)

    for metric_set in sets:
        c("strncpy(dev_priv->perf.oa.test_config.uuid,")
        c.indent(8)
        c("\"" + metric_set['guid'] + "\",")
        c("UUID_STRING_LEN);")
        c.outdent(8)
        c("dev_priv->perf.oa.test_config.id = 1;")
        c("\n")
        c("dev_priv->perf.oa.test_config.mux_regs = mux_config_" + metric_set['perf_name_lc'] + ";")
        c("dev_priv->perf.oa.test_config.mux_regs_len = ARRAY_SIZE(mux_config_" + metric_set['perf_name_lc'] + ");")
        c("\n")
        c("dev_priv->perf.oa.test_config.b_counter_regs = b_counter_config_" + metric_set['perf_name_lc'] + ";")
        c("dev_priv->perf.oa.test_config.b_counter_regs_len = ARRAY_SIZE(b_counter_config_" + metric_set['perf_name_lc'] + ");")
        c("\n")
        c("dev_priv->perf.oa.test_config.flex_regs = flex_eu_config_" + metric_set['perf_name_lc'] + ";")
        c("dev_priv->perf.oa.test_config.flex_regs_len = ARRAY_SIZE(flex_eu_config_" + metric_set['perf_name_lc'] + ");")
        c("\n")
        c("dev_priv->perf.oa.test_config.sysfs_metric.name = \"" + metric_set['guid'] + "\";")
        c("dev_priv->perf.oa.test_config.sysfs_metric.attrs = dev_priv->perf.oa.test_config.attrs;")
        c("\n")
        c("dev_priv->perf.oa.test_config.attrs[0] = &dev_priv->perf.oa.test_config.sysfs_metric_id.attr;")
        c("\n")
        c("dev_priv->perf.oa.test_config.sysfs_metric_id.attr.name = \"id\";")
        c("dev_priv->perf.oa.test_config.sysfs_metric_id.attr.mode = 0444;")
        c("dev_priv->perf.oa.test_config.sysfs_metric_id.show = show_" + metric_set['perf_name_lc'] + "_id;")
    c.outdent(8)
    c("}")


parser = argparse.ArgumentParser()
parser.add_argument("xml", nargs="+", help="XML description of metrics")
parser.add_argument("--guids", required=True, help="Metric set GUID registry")
parser.add_argument("--chipset", required=True, help="Chipset being output for")
parser.add_argument("--c-out", required=True, help="Filename for generated C code")
parser.add_argument("--h-out", required=True, help="Filename for generated header")
parser.add_argument("--sysfs", action="store_true", help="Output code for sysfs")
parser.add_argument("--whitelist", help="Override default metric set whitelist")
parser.add_argument("--no-whitelist", action="store_true", help="Bypass default metric set whitelist")
parser.add_argument("--blacklist", help="Don't generate anything for given metric sets")

args = parser.parse_args()

guids = {}

chipset = args.chipset.upper()

guids_xml = et.parse(args.guids)
for guid in guids_xml.findall(".//guid"):
    if 'config_hash' in guid.attrib:
        hashing_key = oa_registry.Registry.chipset_derive_hash(guid.get('chipset'), guid.get('config_hash'))
        guids[hashing_key] = guid.get('id')

# Note: either filename argument may == None
h = codegen.Codegen(args.h_out);
h.use_tabs = True
c = codegen.Codegen(args.c_out);
c.use_tabs = True

h(copyright)
h("#ifndef __I915_OA_" + chipset + "_H__\n")
h("#define __I915_OA_" + chipset + "_H__\n\n")

c(copyright)

if args.sysfs:
    c("#include <linux/sysfs.h>")
    c("\n")

c("#include \"i915_drv.h\"\n")
c("#include \"i915_oa_" + args.chipset + ".h\"\n")

sets = []

for arg in args.xml:
    xml = et.parse(arg)

    for set_element in xml.findall(".//set"):

        assert set_element.get('chipset') == chipset

        set_name = set_element.get('symbol_name')

        # Exception on Haswell, which doesn't have a test config.
        if chipset == 'HSW':
          if set_name != 'RenderBasic':
              continue
        elif set_name != 'TestOa':
            continue

        if args.whitelist:
            set_whitelist = args.whitelist.split()
            if set_name not in set_whitelist:
                continue

        if args.blacklist:
            set_blacklist = args.blacklist.split()
        else:
            set_blacklist = default_set_blacklist
        if set_name in set_blacklist:
            continue

        configs = set_element.findall("register_config")
        if len(configs) == 0:
            print_err("WARNING: Missing register configuration for set \"" + set_element.get('name') + "\" (SKIPPING)")
            continue

        hw_config_hash = oa_registry.Registry.hw_config_hash(set_element)
        hashing_key = oa_registry.Registry.chipset_derive_hash(chipset.lower(), hw_config_hash)
        if hashing_key not in guids:
            print_err("WARNING: No GUID found for metric set " + chipset + ", " + set_element.get('name') + " (expected key = " + hashing_key + ") (SKIPPING)")
            continue

        perf_name_lc = underscore(set_name)
        perf_name = perf_name_lc.upper()

        metric_set = {
                'name': set_name,
                'set_element': set_element,
                'chipset_lc': chipset.lower(),
                'perf_name_lc': perf_name_lc,
                'perf_name': perf_name,
                'guid': guids[hashing_key],
                'configs': configs
              }
        sets.append(metric_set)

    for metric_set in sets:
        set_name = metric_set['name']
        configs = metric_set['configs']

        mux_configs = []
        b_counter_configs = []
        flex_configs = []
        for config in configs:
            config_type = config.get('type')
            if config_type == "NOA":
                mux_configs.append(config)
            elif config_type == "OA":
                b_counter_configs.append(config)
            elif config_type == "FLEX":
                flex_configs.append(config)

        if len(b_counter_configs) == 0:
            empty = et.Element('register_config')
            b_counter_configs.append(empty)
        assert len(b_counter_configs) == 1
        output_b_counter_config(metric_set, b_counter_configs[0])

        if len(flex_configs) == 0:
            empty = et.Element('register_config')
            flex_configs.append(empty)
        assert len(flex_configs) == 1
        output_flex_config(metric_set, flex_configs[0])

        assert len(mux_configs) == 1
        output_mux_config(metric_set, mux_configs[0])


if args.sysfs:
    output_sysfs_code(sets)

h("#endif\n")
