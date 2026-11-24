import re
from fractions import Fraction

def fields(obj,required,optional=()):
    if not isinstance(obj,dict) or set(required)-obj.keys() or obj.keys()-set(required)-set(optional):
        raise ValueError(f'expected fields {required}; optional {optional}')
    return obj

def array(value,low,high):
    if not isinstance(value,list) or not low<=len(value)<=high:raise ValueError(f'array size {low}..{high} required')
    return value

def integer(value,low,high):
    if type(value) is not int or not low<=value<=high:raise ValueError(f'integer {low}..{high} required')
    return value

def ident(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_:-]{1,64}',value):raise ValueError('invalid id')
    return value

def unique(values):
    if len(set(values))!=len(values):raise ValueError('duplicate identity')

def number(value):
    if not isinstance(value,str) or not re.fullmatch(r'-?(?:0|[1-9][0-9]{0,8})(?:[.][0-9]{1,6})?',value):raise ValueError('bounded decimal string required')
    return Fraction(value)

def vector(value,size):return tuple(number(x) for x in array(value,size,size))

def integer(value, low, high):
    if type(value) is not int or not low<=value<=high: raise ValueError('bounded integer required')
    return value

def fraction_str(value):
    """把 Fraction 精确渲染为十进制字符串；除不尽时沿用 n/d 口径。布尔值不接受。"""
    if not isinstance(value,Fraction):raise ValueError('fraction required')
    sign='-' if value<0 else '';v=abs(value);whole,rem=divmod(v.numerator,v.denominator)
    if rem==0:return f'{sign}{whole}'
    q=v.denominator;tail=0
    while q%2==0:q//=2;tail+=1
    while q%5==0:q//=5;tail+=1
    if q!=1:return str(value)
    digits=rem*10**tail//v.denominator
    return f'{sign}{whole}.{str(digits).zfill(tail)}'
