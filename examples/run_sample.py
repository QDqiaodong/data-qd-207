from seismic_assoc import process
from seismic_assoc.io import load,publish
if __name__=='__main__':
 r=process(load('fixtures/input.json'));publish('run/sample.json',r);print(r['summary'])
