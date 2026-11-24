import json,unittest,copy
from pathlib import Path
from seismic_assoc import process
class T(unittest.TestCase):
 def setUp(self):self.d=json.loads(Path('fixtures/input.json').read_text())
 def test_cluster(self):
  r=process(self.d);self.assertEqual(r['summary'],{'arrivals':3,'events':2,'multi_station':1});self.assertEqual(r['events'][0]['phase_counts'],{'P':2,'S':0})
 def test_order(self):
  x=process(self.d);self.d['events'].reverse();self.assertEqual(process(self.d),x)
 def test_wide_window_merges(self):
  self.d['window']='2';self.d['events'].append({'id':'d','station':'B','phase':'S','time':'11'})
  self.assertEqual(process(self.d)['summary']['events'],2)
 def test_bad_station_phase(self):
  for k,v in [('station','X'),('phase','X')]:
   d=copy.deepcopy(self.d);d['events'][0][k]=v
   with self.assertRaises(ValueError):process(d)
 def test_empty_not_allowed(self):
  self.d['events']=[]
  with self.assertRaises(ValueError):process(self.d)
 def test_fraction_time(self):
  self.d['events'][0]['time']='10.25';self.assertEqual(process(self.d)['events'][0]['anchor'],'41/4')
