"""다양한 생활 운행 배정. 비율·길이 범위는 실차 추정치가 아닌 개발 기본값이다."""
from datetime import datetime, timedelta


def varied_day(req, day, fixed, daily, kind, kst):
    scale=fixed["daily_target_minutes"]/60
    day_factor=daily.uniform(1-req.daily_variation_percent/100,1+req.daily_variation_percent/100)
    start=max(240,min(720,fixed["first_departure_minute"]+daily.randint(-req.departure_jitter_minutes,req.departure_jitter_minutes)+(60 if day.isoweekday()>=6 else 0)))

    def duration(purpose):
        if purpose=="commute":
            value=fixed["commute_minutes"]*day_factor*daily.uniform(.9,1.1)
        elif purpose=="local":
            value=daily.uniform(3,12)*scale**.5*day_factor
        elif purpose=="regular":
            value=daily.uniform(12,35)*scale*day_factor
        else:
            value=daily.uniform(35,95)*scale*day_factor
        return max(2,min(120,round(value)))

    if kind=="commute":
        outward,homeward=duration("commute"),duration("commute")
        back=max(start+outward+30,min(1440-homeward,fixed["evening_departure_minute"]+daily.randint(-req.departure_jitter_minutes,req.departure_jitter_minutes)))
        slots=[(start,outward,"commute"),(back,homeward,"commute")]
        count=daily.choices([0,1,2],weights=[55,35,10])[0]
        for _ in range(count):
            length=duration("local")
            low,high=max(start+outward+30,660),min(back-length-30,900)
            if high<low:continue
            for _ in range(12):
                candidate=daily.randint(low,high)
                if all(candidate+length+30<=a or candidate>=a+b+30 for a,b,_ in slots):
                    slots.append((candidate,length,"local"));break
    else:
        count=daily.choices([1,2,3,4,5],weights=[15,35,30,15,5] if kind=="flexible" else [0,30,40,25,5])[0]
        purposes=daily.choices(["local","regular","long"],weights=[55,35,10],k=count)
        lengths=[duration(p) for p in purposes]
        extra=1440-start-sum(lengths)-30*(count-1)
        slots=[]
        for i,(length,purpose) in enumerate(zip(lengths,purposes)):
            slots.append((start,length,purpose))
            if i<count-1:
                gap=min(extra,daily.randint(0,180 if kind=="flexible" else 90))
                start+=length+30+gap
                extra-=gap

    midnight=datetime(day.year,day.month,day.day,tzinfo=kst)
    for minute,length,purpose in sorted(slots):
        profile=fixed["road_profile"]
        if purpose=="local":profile=daily.choices(["urban","mixed"],weights=[80,20])[0]
        elif profile=="highway" and length<12:profile="mixed"
        elif profile=="congested":profile=daily.choices(["congested","urban","mixed"],weights=[65,15,20])[0]
        departure=midnight+timedelta(minutes=minute)
        arrival=departure+timedelta(minutes=length)
        if not 0<=departure.timestamp()*1000<arrival.timestamp()*1000<=4102444800000:
            raise ValueError("일정 문맥의 운행 시각은1970~2100년 범위여야 합니다.")
        yield {"start":departure.isoformat(),"end":arrival.isoformat(),"road_profile":profile}
