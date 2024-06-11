""" from https://github.com/keithito/tacotron """

'''
Cleaners are transformations that run over the input text at both training and eval time.

Cleaners can be selected by passing a comma-delimited list of cleaner names as the "cleaners"
hyperparameter. Some cleaners are English-specific. You'll typically want to use:
  1. "english_cleaners" for English text
  2. "transliteration_cleaners" for non-English text that can be transliterated to ASCII using
     the Unidecode library (https://pypi.python.org/pypi/Unidecode)
  3. "basic_cleaners" if you do not want to transliterate (in this case, you should also update
     the symbols in symbols.py to match your data).
'''

import re
from unidecode import unidecode
from .numbers import normalize_numbers


# Regular expression matching whitespace:
_whitespace_re = re.compile(r'\s+')

# List of (regular expression, replacement) pairs for abbreviations:
_abbreviations = [(re.compile('\\b%s\\.' % x[0], re.IGNORECASE), x[1]) for x in [
  ('mrs', 'misess'),
  ('mr', 'mister'),
  ('dr', 'doctor'),
  ('st', 'saint'),
  ('co', 'company'),
  ('jr', 'junior'),
  ('maj', 'major'),
  ('gen', 'general'),
  ('drs', 'doctors'),
  ('rev', 'reverend'),
  ('lt', 'lieutenant'),
  ('hon', 'honorable'),
  ('sgt', 'sergeant'),
  ('capt', 'captain'),
  ('esq', 'esquire'),
  ('ltd', 'limited'),
  ('col', 'colonel'),
  ('ft', 'fort'),
]]


def expand_abbreviations(text):
  for regex, replacement in _abbreviations:
    text = re.sub(regex, replacement, text)
  return text


def expand_numbers(text):
  return normalize_numbers(text)


def lowercase(text):
  return text.lower()


def collapse_whitespace(text):
  return re.sub(_whitespace_re, ' ', text)


def convert_to_ascii(text):
  return unidecode(text)


def basic_cleaners(text):
  '''Basic pipeline that lowercases and collapses whitespace without transliteration.'''
  text = lowercase(text)
  text = collapse_whitespace(text)
  return text


def transliteration_cleaners(text):
  '''Pipeline for non-English text that transliterates to ASCII.'''
  text = convert_to_ascii(text)
  text = lowercase(text)
  text = collapse_whitespace(text)
  return text


# List of (regular expression, replacement) pairs for abbreviations:
_ckb_transliterate = [(re.compile('\\b%s\\.' % x[0], re.IGNORECASE), x[1]) for x in [
('ؤ','o'),
('ئ','eh'),
('ا','aa'),
('ب','b'),
('ت','t'),
('ث','th'),
('ج','j'),
('ح','he'),
('خ','kh'),
('د','d'),
('ر','r'),
('ز','z'),
('س','s'),
('ش','sh'),
('ع','a\''),
('غ','gh'),
('ف','f'),
('ق','q'),
('ك','k'),
('ل','l'),
('م','m'),
('ن','n'),
('ه','eh'),
('و','oo'),
('ى','ee'),
('ي','ee'),
('پ','p'),
('چ','ch'),
('ڕ','rr'),
('ژ','zh'),
('ڤ','v'),
('ک','k'),
('گ','g'),
('ڵ','ll'),
('ھ','h'),
('ۆ','o'),
('ی','y'),
('ێ','y'),
('ە','eh')
]]


def ckb_transliterate(text):
  for regex, replacement in _ckb_transliterate:
    text = re.sub(regex, replacement, text)
  return text


def english_cleaners(text):
  '''Pipeline for English text, including number and abbreviation expansion.'''
  text = ckb_transliterate(text)
  text = convert_to_ascii(text)
  text = lowercase(text)
  text = expand_numbers(text)
  text = expand_abbreviations(text)
  text = collapse_whitespace(text)
  text = text.replace('"', '')
  return text
