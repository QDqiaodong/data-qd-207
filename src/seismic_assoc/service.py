from .checks import fields,array,ident,number

def process(data):
 fields(data,('stations','events','window'));stations={ident(s['id']):(number(s['lat']),number(s['lon'])) for s in array(data['stations'],1,100)};window=number(data['window'])
 events=[]
 for e in array(data['events'],1,2000):
  fields(e,('id','station','phase','time'));k=ident(e['id']);st=ident(e['station']);ph=ident(e['phase']);t=number(e['time'])
  if st not in stations or ph not in ('P','S'):raise ValueError('invalid arrival')
  events.append((k,st,ph,t))
 events.sort(key=lambda x:(x[3],x[0]));clusters=[]
 for row in events:
  placed=[]
  for i,c in enumerate(clusters):
   if abs(row[3]-c['anchor'])<=window:placed.append(i)
  if len(placed)>1:raise ValueError('arrival belongs to overlapping event windows')
  if placed:clusters[placed[0]]['arrivals'].append(row)
  else:clusters.append({'anchor':row[3],'arrivals':[row]})
 result=[]
 for i,c in enumerate(clusters,1):
  arr=c['arrivals'];result.append({'event_id':f'e{i}','anchor':str(c['anchor']),'arrivals':[{'id':a,'station':s,'phase':p,'time':str(t)} for a,s,p,t in arr],'stations':sorted({s for _,s,_,_ in arr}),'phase_counts':{p:sum(x[2]==p for x in arr) for p in ('P','S')}})
 return {'events':result,'summary':{'arrivals':len(events),'events':len(result),'multi_station':sum(len(e['stations'])>1 for e in result)}}
