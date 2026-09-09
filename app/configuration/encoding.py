"""Strict text parsing and canonical encoding, without filesystem/environment IO."""

from decimal import Decimal
import hashlib
import json
import re

import yaml

from .models import ConfigurationError, decimal_input


def plain(value):
    if isinstance(value, Decimal):
        # No normalize(): its rounding would depend on the caller Decimal context.
        s = format(value, 'f')
        return s.rstrip('0').rstrip('.') if '.' in s else s
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(v) for v in value]
    return value


def canonical(value):
    return json.dumps(plain(value), sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


def text_hash(text):
    return hashlib.sha256(text.encode('utf8')).hexdigest()


def content_hash(value):
    return text_hash(canonical(value))


class _Loader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if type(key) is not str or key == '<<' or key in result:
                raise ConfigurationError('Duplicate, merged or non-string YAML key')
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def _bool(loader, node):
    if node.value not in ('true', 'false'):
        raise ConfigurationError('Only literal true/false YAML booleans are accepted')
    return node.value == 'true'


def _int(loader, node):
    if not re.fullmatch(r'[-+]?(0|[1-9][0-9]*)', node.value):
        raise ConfigurationError('Integer must use explicit decimal notation, not octal/hex/sexagesimal')
    value=int(node.value)
    decimal_input(value)
    return value


_Loader.add_constructor('tag:yaml.org,2002:float', lambda loader, node: decimal_input(node.value))
_Loader.add_constructor('tag:yaml.org,2002:bool', _bool)
_Loader.add_constructor('tag:yaml.org,2002:int', _int)


def parse_text(text):
    if type(text) is not str or len(text.encode('utf8')) > 1_000_000:
        raise ConfigurationError('Explicit bounded YAML text required')
    if re.search(r'\$\{|\$[A-Za-z_]|\{\{|\}\}|%[A-Za-z_][A-Za-z0-9_]*%', text):
        raise ConfigurationError('Environment/template interpolation is forbidden')
    tokens = list(yaml.scan(text))
    if len(tokens) > 30000 or any(isinstance(t, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken, yaml.tokens.TagToken)) for t in tokens):
        raise ConfigurationError('Aliases, anchors, tags or excessive YAML are forbidden')
    result = yaml.load(text, Loader=_Loader)
    def check(value, depth=0):
        if depth > 24:
            raise ConfigurationError('Configuration nesting exceeds limit')
        if isinstance(value, dict):
            for v in value.values(): check(v, depth+1)
        elif isinstance(value, list):
            for v in value: check(v, depth+1)
        elif not isinstance(value, (str, int, Decimal, bool, type(None))):
            raise ConfigurationError('Unsupported scalar type; timestamps must be UTC seconds')
    check(result)
    if type(result) is not dict:
        raise ConfigurationError('Explicit YAML mapping required')
    return result


def flatten(value, prefix=''):
    if isinstance(value, dict):
        for key in sorted(value):
            yield from flatten(value[key], prefix+'.'+key if prefix else key)
    elif isinstance(value, (tuple, list)):
        if not value:
            yield prefix, []
        for index, child in enumerate(value):
            yield from flatten(child, prefix+'.'+str(index))
    else:
        yield prefix, value
