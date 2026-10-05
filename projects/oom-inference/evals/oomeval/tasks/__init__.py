from .choice import GPQADiamond, MMLUPro
from .math import AIME, AIME25, GSM8K
from .ruler import Ruler

TASKS = {t.name: t for t in (GSM8K, AIME, AIME25, GPQADiamond, MMLUPro, Ruler)}
