import unittest,json,copy
from pathlib import Path
from seismic_assoc import process
class Regression(unittest.TestCase):
 def test_anchor_not_transitive_and_duplicate(self):
  d=json.loads(Path('fixtures/input.json').read_text());d['window']='0.5';d['events']=[{'id':str(i),'station':'A','phase':'P','time':t} for i,t in enumerate(['0','0.4','0.8'])]
  self.assertEqual([len(e['arrivals']) for e in process(d)['events']],[2,1])
  d['events'].append(copy.deepcopy(d['events'][0]))
  with self.assertRaises(ValueError):process(d)
