from .checks import fields,array,ident,number

def process(data):
 fields(data,('stations','events','window'));stations={};window=number(data['window'])
 if window<0:raise ValueError('window must be nonnegative')
 for station in array(data['stations'],1,100):
  fields(station,('id','lat','lon'));key=ident(station['id']);lat=number(station['lat']);lon=number(station['lon'])
  if key in stations or not -90<=lat<=90 or not -180<=lon<=180:raise ValueError('invalid station')
  stations[key]=(lat,lon)
 events=[]
 for e in array(data['events'],1,2000):
  fields(e,('id','station','phase','time'));k=ident(e['id']);st=ident(e['station']);ph=ident(e['phase']);t=number(e['time'])
  if st not in stations or ph not in ('P','S'):raise ValueError('invalid arrival')
  if any(e[0]==k for e in events):raise ValueError('duplicate arrival')
  events.append((k,st,ph,t))
 events.sort(key=lambda x:(x[3],x[0]));clusters=[]
 for row in events:
  if clusters and row[3]-clusters[-1]['anchor']<=window:clusters[-1]['arrivals'].append(row)
  else:clusters.append({'anchor':row[3],'arrivals':[row]})
 result=[]
 for i,c in enumerate(clusters,1):
  arr=c['arrivals'];result.append({'event_id':f'e{i}','anchor':str(c['anchor']),'arrivals':[{'id':a,'station':s,'phase':p,'time':str(t)} for a,s,p,t in arr],'stations':sorted({s for _,s,_,_ in arr}),'phase_counts':{p:sum(x[2]==p for x in arr) for p in ('P','S')}})
 return {'events':result,'summary':{'arrivals':len(events),'events':len(result),'multi_station':sum(len(e['stations'])>1 for e in result)}}
