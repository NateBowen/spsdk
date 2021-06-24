#!/usr/bin/env python
# -*- coding: UTF-8 -*-
#
# Copyright 2020-2021 NXP
#
# SPDX-License-Identifier: BSD-3-Clause

"""Module provides support for Protected Flash Region areas (CMPA, CFPA)."""
import copy
import json
import logging
import math
import os
from typing import Any, Dict, List, Optional, Union

import yaml
from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePublicKey
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from ruamel.yaml.comments import CommentedMap as CM

from spsdk import __author__ as spsdk_author
from spsdk import __release__ as spsdk_release
from spsdk import __version__ as spsdk_version
from spsdk.utils.crypto.abstract import BackendClass
from spsdk.utils.crypto.backend_openssl import openssl_backend
from spsdk.utils.exceptions import SPSDKRegsErrorRegisterNotFound
from spsdk.utils.misc import change_endianism, format_value, value_to_int
from spsdk.utils.reg_config import RegConfig
from spsdk.utils.registers import Registers, RegsBitField, RegsEnum, RegsRegister

from . import PFR_DATA_FOLDER
from .exceptions import (
    SPSDKPfrConfigError,
    SPSDKPfrConfigReadError,
    SPSDKPfrError,
    SPSDKPfrRotkhIsNotPresent,
)

logger = logging.getLogger(__name__)


class PfrConfiguration:
    """Class to open PFR configuration file a get basic configuration."""

    def __init__(
        self,
        config: Union[str, dict, "PfrConfiguration"] = None,
        device: str = None,
        revision: str = None,
        cfg_type: str = None,
    ) -> None:
        """Open config PFR file.

        :param config: Filename or dictionary with PFR settings, defaults to None
        :param device: If needed it could be used to override device from settings, defaults to ""
        :param revision: If needed it could be used to override revision from settings, defaults to ""
        :param cfg_type: If needed it could be used to override PFR type from settings, defaults to ""
        """
        self.device = device
        self.revision = revision
        self.type = cfg_type
        self.settings: Optional[Union[CM, dict]] = None

        if isinstance(config, (str, os.PathLike, dict)):
            self.set_config(config)

        if isinstance(config, PfrConfiguration):
            if not self.device:
                self.device = config.device
            if not self.revision:
                self.revision = config.revision
            if not self.type:
                self.type = config.type
            if config.settings:
                self.settings = config.settings.copy()

    def _detect_obsolete_style_of_settings(self, data: Union[CM, dict]) -> bool:
        """Detect obsolete style of configuration.

        :param data: As old JSON style as new YML style of settings data.
        :return: True if obsolete style is detected.
        """
        if len(data) == 0:
            return False

        for key in data.keys():
            if isinstance(data[key], (str, int)):
                return True
            if isinstance(data[key], dict):
                first_key = list(data[key].keys())[0]
                if first_key not in ("value", "bitfields", "name"):
                    return True

        return False

    def _get_yml_style_of_settings(self, data: Union[CM, dict]) -> Union[CM, dict]:
        """Get unified YML style of settings.

        :param data: As old JSON style as new YML style of settings data.
        :return: New YML style of data.
        """
        if not self._detect_obsolete_style_of_settings(data):
            return data

        yml_style: Dict[str, Union[str, int, dict]] = {}
        for key, val in data.items():
            if isinstance(val, (str, int)):
                yml_style[key] = {"value": val}
            if isinstance(val, dict):
                bitfields = {}
                for key_b, val_b in val.items():
                    bitfields[key_b] = val_b
                yml_style[key] = {"bitfields": bitfields}

        return yml_style

    def set_config_dict(
        self,
        data: Union[CM, dict],
        device: str = None,
        revision: str = None,
        cfg_type: str = None,
    ) -> None:
        """Apply configuration dictionary.

        The function accepts as dictionary as from commented map.

        :param data: Settings of PFR.
        :param device: If needed it could be used to override device from settings, defaults to ""
        :param revision: If needed it could be used to override revision from settings, defaults to ""
        :param cfg_type: If needed it could be used to override PFR type from settings, defaults to ""
        :raises SPSDKPfrConfigReadError: Invalid YML file.
        """
        if data is None or len(data) == 0:
            raise SPSDKPfrConfigReadError("Empty YAML configuration.")

        try:
            description = data.get("description", data)
            self.device = device or description.get("device", None)
            self.revision = revision or description.get("revision", None)
            self.type = cfg_type or description.get("type", None)
            self.settings = self._get_yml_style_of_settings(data["settings"])

        except KeyError as exc:
            raise SPSDKPfrConfigReadError("Missing fields in YAML configuration.") from exc

    def set_config_json(self, file_name: str) -> None:
        """Apply JSON configuration from file.

        :param file_name: Name of JSON configuration file.
        :raises SPSDKPfrConfigReadError: Invalid JSON file.
        """
        try:
            with open(file_name, "r") as file_json:
                data = json.load(file_json)
        except (FileNotFoundError, TypeError, ValueError) as exc:
            raise SPSDKPfrConfigReadError(
                f"Cannot load JSON configuration file. ({file_name}) - {exc}"
            ) from exc

        try:
            self.set_config_dict(data)
        except SPSDKPfrConfigReadError as exc:
            raise SPSDKPfrConfigReadError(
                f"Decoding error({str(exc)}) with JSON configuration file. ({file_name})"
            ) from exc

    def set_config_yml(self, file_name: str) -> None:
        """Apply YML configuration from file.

        :param file_name: Name of YML configuration file.
        :raises SPSDKPfrConfigReadError: Invalid YML commented map.
        """
        try:
            with open(file_name, "r") as file_yml:
                yml_raw = file_yml.read()
            data = yaml.safe_load(yml_raw)
        except (FileNotFoundError, TypeError, ValueError) as exc:
            raise SPSDKPfrConfigReadError(
                f"Cannot load YAML configuration file. ({file_name})."
            ) from exc

        try:
            self.set_config_dict(data)
        except SPSDKPfrConfigReadError as exc:
            raise SPSDKPfrConfigReadError(
                f"Decoding error with YAML configuration file. ({file_name})"
            ) from exc

    def set_config(self, config: Union[str, CM, dict]) -> None:
        """Apply configuration from file.

        :param config: Name of configuration file or Commented map.
        """
        if isinstance(config, (CM, dict)):
            self.set_config_dict(config)
        else:
            extension = os.path.splitext(config)[1]
            # Try open configuration file by its extensions
            if extension == ".json":
                self.set_config_json(config)
            elif extension in (".yml", ".yaml"):
                self.set_config_yml(config)
            else:
                # Just try to open one by one to be lucky
                try:
                    self.set_config_json(config)
                except SPSDKPfrConfigReadError:
                    self.set_config_yml(config)

    def get_yaml_config(self, data: CM, indent: int = 0) -> CM:
        """Return YAML configuration In PfrConfiguration format.

        :param data: The registers settings data.
        :param indent: YAML start indent.
        :return: YAML PFR configuration in commented map(ordered dict).
        """
        assert self.device
        assert self.type
        res_data = CM()

        res_data.yaml_set_start_comment(
            f"NXP {self.device} PFR {self.type} configuration", indent=indent
        )

        description = CM()
        description.insert(1, "device", self.device, comment="The NXP device name.")
        description.insert(2, "revision", self.revision, comment="The NXP device revision.")
        description.insert(3, "type", self.type.upper(), comment="The PFR type (CMPA, CFPA).")
        description.insert(4, "version", spsdk_version, comment="The SPSDK tool version.")
        description.insert(5, "author", spsdk_author, comment="The author of the configuration.")
        description.insert(6, "release", spsdk_release, comment="The SPSDK release.")

        res_data.insert(
            1,
            "description",
            description,
            comment=f"The PFR {self.type} configuration description.",
        )
        res_data.insert(
            2, "settings", data, comment=f"The PFR {self.type} registers configuration."
        )
        return res_data

    def is_invalid(self) -> Optional[str]:
        """Validate configuration.

        :return: None if configuration is valid, otherwise description string what is invalid.
        """
        if not self.device:
            return "The device is NOT specified!"
        if not self.type:
            return "The PFR type (CMPA/CFPA) is NOT specified!"

        return None

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, PfrConfiguration):
            return False

        return vars(self) == vars(other)


class BaseConfigArea:
    """Base for CMPA and CFPA classes."""

    CONFIG_DIR = PFR_DATA_FOLDER
    CONFIG_FILE = "database.json"
    BINARY_SIZE = 512
    ROTKH_SIZE = 32
    ROTKH_REGISTER = "ROTKH"
    MARK = b"SEAL"
    DESCRIPTION = "Base Config Area"

    def __init__(
        self, device: str = None, revision: str = None, user_config: PfrConfiguration = None
    ) -> None:
        """Initialize an instance.

        :param device: device to use, list of supported devices is available via 'devices' method
        :param revision: silicon revision, if not specified, the latest is being used
        :param user_config: PfrConfiguration with user configuration to use with initialization
        """
        self.bc_cfg = None
        assert device or user_config
        self.config = self._load_config()
        self.device = device or (user_config.device if user_config else "")

        assert self.device in self.config.get_devices(), f"Device '{self.device}' is not supported"
        self.revision = revision or (user_config.revision if user_config else "latest")
        if not self.revision or self.revision == "latest":
            self.revision = self.config.get_latest_revision(self.device)
            logger.warning(
                f"The silicon revision is not specified, the latest: '{self.revision}' has been used."
            )

        assert self.revision in self.config.get_revisions(
            self.device
        ), f"Invalid revision '{self.revision}' for '{self.device}'"
        self.registers = Registers(self.device)
        self.registers.load_registers_from_xml(
            xml=self.config.get_data_file(self.device, self.revision),
            filter_reg=self.config.get_ignored_registers(self.device),
            grouped_regs=self.config.get_grouped_registers(self.device),
        )

        # Set the computed field handler
        for reg, fields in self.config.get_computed_fields(self.device).items():
            reg_obj = self.registers.find_reg(reg)
            reg_obj.add_setvalue_hook(self.reg_computed_fields_handler, fields)

        # Solve backward compatibility of configuration
        self._apply_backward_compatibility()

        self.user_config = PfrConfiguration(
            config=user_config,
            device=self.device,
            revision=self.revision,
            cfg_type=self.__class__.__name__,
        )

        if self.user_config.settings:
            self.set_config(self.user_config, raw=False)

    def reg_computed_fields_handler(self, val: bytes, context: Any) -> bytes:
        """Recalculate all fields for given register value.

        :param val: Input register value.
        :param context: The method context (fields).
        :return: recomputed value.
        :raises SPSDKPfrError: Raises when the computing routine is not found.
        """
        fields: dict = context
        for method in fields.values():
            if hasattr(self, method):
                method_ref = getattr(self, method, None)
                val = method_ref(val)
            else:
                raise SPSDKPfrError(f"The '{method}' compute function doesn't exists.")

        return val

    @staticmethod
    def pfr_reg_inverse_high_half(val: bytes) -> bytes:
        """Function that inverse low 16-bits of register value to high 16 bits.

        :param val: Input current reg value.
        :return: Returns the complete register value with updated higher half field.
        """
        ret = bytearray(val)
        ret[0] = ret[2] ^ 0xFF
        ret[1] = ret[3] ^ 0xFF
        return bytes(ret)

    @classmethod
    def _load_config(cls) -> RegConfig:
        """Loads the PFR block configuration file.

        :return: PFR block configuration database.
        """
        return RegConfig(os.path.join(cls.CONFIG_DIR, cls.CONFIG_FILE))

    @classmethod
    def devices(cls) -> List[str]:
        """Classmethod to get list of supported devices.

        :return: List of supported devices.
        """
        config = cls._load_config()
        return config.get_devices()

    def _get_registers(self) -> List[RegsRegister]:
        """Get a list of all registers.

        :return: List of PFR configuration registers.
        """
        exclude = self.config.get_ignored_registers(self.device)
        return self.registers.get_registers(exclude)

    def set_config(self, config: PfrConfiguration, raw: bool = False) -> None:
        """Apply configuration from file.

        :param config: PFR configuration.
        :param raw: When set all (included computed fields) configuration will be applied.
        :raises SPSDKPfrConfigError: Invalid config file.
        """
        assert self.device
        assert self.revision

        if config.device != self.device:
            raise SPSDKPfrConfigError(
                f"Invalid device in configuration. {self.device} != {config.device}"
            )
        if not config.revision or config.revision in ("latest", ""):
            config.revision = self.config.get_latest_revision(self.device)
            logger.warning(
                f"The configuration file doesn't contains silicon revision, \
the latest: '{config.revision}' has been used."
            )
        if config.revision != self.revision:
            raise SPSDKPfrConfigError(
                f"Invalid revision in configuration. {self.revision} != {config.revision}"
            )
        if config.type and config.type.upper() != self.__class__.__name__:
            raise SPSDKPfrConfigError(
                f"Invalid configuration type. {self.__class__.__name__} != {config.type}"
            )

        if not config.settings:
            raise SPSDKPfrConfigError("Missing configuration of PFR fields!")

        computed_regs = []
        computed_regs.extend(self.config.get_ignored_registers(self.device))
        if not raw:
            computed_regs.extend(self.config.get_computed_registers(self.device))
        computed_fields = None if raw else self.config.get_computed_fields(self.device)

        self.registers.load_yml_config(config.settings, computed_regs, computed_fields)
        if not raw:
            # Just update only configured registers
            exclude_hooks = list(set(self.registers.get_reg_names()) - set(config.settings.keys()))
            self.registers.run_hooks(exclude_hooks)

    def get_yaml_config(
        self, exclude_computed: bool = True, diff: bool = False, indent: int = 0
    ) -> CM:
        """Return YAML configuration from loaded registers.

        :param exclude_computed: Omit computed registers and fields.
        :param diff: Get only configuration with difference value to reset state.
        :param indent: YAML start indent.
        :return: YAML PFR configuration in commented map(ordered dict).
        """
        computed_regs = (
            None if not exclude_computed else self.config.get_computed_registers(self.device)
        )
        computed_fields = (
            None if not exclude_computed else self.config.get_computed_fields(self.device)
        )
        ignored_fields = self.config.get_ignored_fields(self.device)

        data = self.registers.create_yml_config(
            computed_regs, computed_fields, ignored_fields, diff, indent + 2
        )
        return self.user_config.get_yaml_config(data, indent)

    def generate_config(self, exclude_computed: bool = True) -> CM:
        """Generate configuration structure for user configuration.

        :param exclude_computed: Exclude computed fields, defaults to True.
        :return: YAML commented map with PFR configuration  in reset state.
        """
        # Create own copy to keep self as is and get reset values by standard YML output
        copy_of_self = copy.deepcopy(self)
        copy_of_self.registers.reset_values()

        return copy_of_self.get_yaml_config(exclude_computed)

    def _calc_rotkh(self, keys: Union[List[RSAPublicKey], List[EllipticCurvePublicKey]]) -> bytes:
        """Calculate ROTKH (Root Of Trust Key Hash).

        :param keys: List of Keys to compute ROTKH.
        :return: Value of ROTKH with right width.
        :raises SPSDKPfrError: Algorithm width doesn't fit into ROTKH field.
        """
        # the data structure use for computing final ROTKH is 4*32B long
        # 32B is a hash of individual keys
        # 4 is the max number of keys, if a key is not provided the slot is filled with '\x00'
        # The niobe4analog has two options to compute ROTKH, so it's needed to be
        # detected the right algorithm and mandatory warn user about this selection because
        # it's MUST correspond to settings in eFuses!
        reg_rotkh = self.registers.find_reg("ROTKH")
        width = reg_rotkh.width
        if isinstance(keys[0], RSAPublicKey):
            algorithm_width = 256
        else:
            algorithm_width = keys[0].key_size

        if algorithm_width > width:
            raise SPSDKPfrError("The ROTKH field is smaller than used algorithm width.")

        key_hashes = [calc_pub_key_hash(key, openssl_backend, algorithm_width) for key in keys]
        data = [
            key_hashes[i] if i < len(key_hashes) else bytes(algorithm_width // 8) for i in range(4)
        ]
        return openssl_backend.hash(bytearray().join(data), f"sha{algorithm_width}").ljust(
            width // 8, b"\x00"
        )

    def _get_seal_start_address(self) -> int:
        """Function returns start of seal fields for the device.

        :return: Start of seals fields.
        """
        start = self.config.get_seal_start_address(self.device)
        assert start, "Can't find 'seal_start_address' in database.json"
        return self.registers.find_reg(start).offset

    def _get_seal_count(self) -> int:
        """Function returns seal count for the device.

        :return: Count of seals fields.
        """
        count = self.config.get_seal_count(self.device)
        assert count, "Can't find 'seal_count' in database.json"
        return value_to_int(count)

    def export(self, add_seal: bool = False, keys: List[RSAPublicKey] = None) -> bytes:
        """Generate binary output.

        :param add_seal: The export is finished in the PFR record by seal.
        :param keys: List of Keys to compute ROTKH field.
        :return: Binary block with PFR configuration(CMPA or CFPA).
        :raises SPSDKPfrRotkhIsNotPresent: This PFR block doesn't contains ROTKH field.
        """
        if keys:
            try:
                # ROTKH may or may not be present, derived class defines its presense
                rotkh_reg = self.registers.find_reg(self.ROTKH_REGISTER)
                rotkh_data = self._calc_rotkh(keys)
                rotkh_reg.set_value(rotkh_data, True)
            except SPSDKRegsErrorRegisterNotFound as exc:
                raise SPSDKPfrRotkhIsNotPresent(
                    "This device doesn't contain ROTKH register!"
                ) from exc

        data = bytearray(self.BINARY_SIZE)
        for reg in self._get_registers():
            # rewriting 4B at the time
            if reg.has_group_registers():
                for grp_reg in reg.sub_regs:
                    val = (
                        grp_reg.get_value()
                        if grp_reg.reverse
                        else change_endianism(bytearray(grp_reg.get_value()))
                    )
                    data[grp_reg.offset : grp_reg.offset + grp_reg.width // 8] = val
            else:
                val = (
                    reg.get_value() if reg.reverse else change_endianism(bytearray(reg.get_value()))
                )
                data[reg.offset : reg.offset + reg.width // 8] = val

        if add_seal:
            seal_start = self._get_seal_start_address()
            seal_count = self._get_seal_count()
            data[seal_start : seal_start + seal_count * 4] = self.MARK * seal_count

        assert (
            len(data) == self.BINARY_SIZE
        ), f"The size of data is {len(data)}, is not equal to {self.BINARY_SIZE}"
        return bytes(data)

    def parse(self, data: bytes) -> None:
        """Parse input binary data to registers.

        :param data: Input binary data of PFR block.
        """
        for reg in self._get_registers():
            value = bytearray(data[reg.offset : reg.offset + reg.width // 8])
            reg.set_value(change_endianism(value), raw=True)

    def _bc_bitfields(self, reg: RegsRegister, bitfield: RegsBitField) -> List[str]:
        """Function returns list of backward compatibility names for bitfield.

        :param reg: The current register
        :param bitfield: Current bitfield
        :return: List of backward compatibility names
        """
        bc_config: Dict = self.config.get_value("backward_compatibility", self.device)
        return bc_config[reg.name]["bitfields"].get(bitfield.name, [])

    def _bc_enums(self, reg: RegsRegister, bitfield: RegsBitField, enum: RegsEnum) -> List[str]:
        """Function returns list of backward compatibility names for enums.

        :param reg: The current register
        :param bitfield: Current bitfield
        :param enum: Current enum
        :return: List of backward compatibility names
        """
        assert self.bc_cfg
        ret = []
        bitfield_n = [bitfield.name]
        reg_n = [reg.name]
        reg = self.bc_cfg.get(reg.name, None)
        if reg:
            # This piece of code is ready for use when also the register names should be backward compatible
            # reg_n.extend(reg.get("name", []))
            #     if "bitfields" in reg.keys():
            bitfield_n.extend(reg["bitfields"].get(bitfield.name, []))

        for r in reg_n:
            for b in bitfield_n:
                ret.append(f"{r}_{b}_VALUE_{enum.get_value_int()}")
                if bitfield.width == 1:
                    ret.append(f"{r}_{b}_{'ENABLE' if enum.get_value_int() == 1 else 'DISABLE'}")

        return ret

    def _apply_backward_compatibility(self) -> None:
        """Apply backward compatibility feature for configuration files."""
        self.bc_cfg = self.config.get_value("backward_compatibility", self.device)
        if self.bc_cfg:
            for bc_reg_name in self.bc_cfg:
                # This piece of code is ready for use when also the register names should be backward compatible
                # if "bitfields" in self.bc_cfg[bc_reg_name].keys():
                bc_reg = self.registers.find_reg(bc_reg_name)
                bc_reg.enable_backward_compatibility(self._bc_bitfields)

            self.registers.enable_backward_compatibility_enums(self._bc_enums)


class CMPA(BaseConfigArea):
    """Customer Manufacturing Configuration Area."""

    CONFIG_DIR = os.path.join(BaseConfigArea.CONFIG_DIR, "cmpa")
    DESCRIPTION = "Customer Manufacturing Programable Area"


class CFPA(BaseConfigArea):
    """Customer In-Field Configuration Area."""

    CONFIG_DIR = os.path.join(BaseConfigArea.CONFIG_DIR, "cfpa")
    DESCRIPTION = "Customer In-field Programmable Area"


def calc_pub_key_hash(
    public_key: Union[RSAPublicKey, EllipticCurvePublicKey],
    backend: BackendClass = openssl_backend,
    sha_width: int = 256,
) -> bytes:
    """Calculate a hash out of public key's exponent and modulus in RSA case, X/Y in EC.

    :param public_key: List of public keys to compute hash from.
    :param backend: Crypto subsystem backend.
    :param sha_width: Used hash algorithm.
    :return: Computed hash.
    """
    if isinstance(public_key, RSAPublicKey):
        n_1 = public_key.public_numbers().e  # type: ignore # MyPy is unable to pickup the class member
        n1_len = math.ceil(n_1.bit_length() / 8)
        n_2 = public_key.public_numbers().n  # type: ignore # MyPy is unable to pickup the class member
        n2_len = math.ceil(n_2.bit_length() / 8)
    else:
        n_1 = public_key.public_numbers().y  # type: ignore # MyPy is unable to pickup the class member
        n1_len = sha_width // 8
        n_2 = public_key.public_numbers().x  # type: ignore # MyPy is unable to pickup the class member
        n2_len = sha_width // 8

    n1_bytes = n_1.to_bytes(n1_len, "big")
    n2_bytes = n_2.to_bytes(n2_len, "big")

    return backend.hash(n2_bytes + n1_bytes, algorithm=f"sha{sha_width}")
