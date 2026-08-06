# -*- coding: utf-8 -*-
"""Channel registry: name -> Channel class."""

_REGISTRY = {}


def register(channel_cls):
    _REGISTRY[channel_cls.name] = channel_cls
    return channel_cls


def get_channel(name):
    return _REGISTRY.get(name)


def available_channels():
    return sorted(_REGISTRY)
