"""계약에서 준비한 정상 경로 검사와 변경 불가능한 전송 본문."""
from dataclasses import dataclass
import json
import math
import re


@dataclass(frozen=True,slots=True)
class WireMessage:
    text: str
    size: int
    timestamp: int | float

    def __getitem__(self,key):
        if key=="ts":return self.timestamp
        raise KeyError(key)


def prepare_message(message):
    raw=json.dumps(message,ensure_ascii=False,separators=(",",":"))
    return WireMessage(raw,len(raw.encode("utf-8")),message["ts"])


def encode_command(command,raw=True):
    message=command.get("message")
    if isinstance(message,WireMessage):
        base=json.dumps({k:v for k,v in command.items() if k!="message"},ensure_ascii=False,separators=(",",":"))
        separator="," if base!="{}" else ""
        key='"message_raw":' if raw else '"message":'
        return base[:-1]+separator+key+message.text+'}'
    return json.dumps(command,ensure_ascii=False,separators=(",",":"))


SUPPORTED={"$schema","title","default","type","const","minimum","maximum","properties",
           "required","additionalProperties","anyOf","pattern","maxLength"}


def compile_check(schema):
    """지원하지 않는 규칙은 빠른 경로를 끄고 기존 JSON Schema 검사로 돌린다."""
    if not isinstance(schema,dict) or set(schema)-SUPPORTED:return None
    kind=schema.get("type")
    if kind not in (None,"object","integer","number","string"):return None
    if "const" in schema and type(schema["const"]) not in (int,float,str,bool,type(None)):return None
    properties=tuple((key,compile_check(value)) for key,value in schema.get("properties",{}).items())
    alternatives=tuple(compile_check(value) for value in schema.get("anyOf",()))
    if any(check is None for _,check in properties) or any(check is None for check in alternatives):return None
    if "additionalProperties" in schema and type(schema["additionalProperties"]) is not bool:return None
    required=frozenset(schema.get("required",()))
    allowed=frozenset(key for key,_ in properties)
    extra=schema.get("additionalProperties",True)
    pattern=re.compile(schema["pattern"]) if "pattern" in schema else None
    minimum,maximum=schema.get("minimum"),schema.get("maximum")
    max_length=schema.get("maxLength")
    constant=schema.get("const")
    has_constant="const" in schema

    def check(value):
        typ=type(value)
        if typ not in (dict,list,str,int,float,bool,type(None)):return False
        numeric=typ in (int,float)
        if kind=="object" and typ is not dict:return False
        if kind=="integer" and not (typ is int or typ is float and math.isfinite(value) and value.is_integer()):return False
        if kind=="number" and not numeric:return False
        if kind=="string" and typ is not str:return False
        if typ is float and not math.isfinite(value):return False
        if has_constant:
            if type(constant) in (int,float):
                if not numeric or value!=constant:return False
            elif typ is not type(constant) or value!=constant:return False
        if numeric:
            if minimum is not None and value<minimum:return False
            if maximum is not None and value>maximum:return False
        if typ is str:
            if max_length is not None and len(value)>max_length:return False
            if pattern is not None and pattern.search(value) is None:return False
        if typ is dict:
            if not required.issubset(value) or not extra and not allowed.issuperset(value):return False
            for name,child in properties:
                if name in value and not child(value[name]):return False
        if alternatives and not any(branch(value) for branch in alternatives):return False
        return True
    return check
