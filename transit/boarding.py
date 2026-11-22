"""Local linear reference on a published, directed GTFS shape around a platform."""
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class PlatformPath:
    lat: float
    lon: float
    longitude_scale: float
    segments: tuple

    @classmethod
    def build(cls, points, stops, index, platform):
        start, anchor, end = stops[max(0,index-8)], stops[index], stops[index+1]
        if end <= anchor:
            return None
        lat,lon = platform
        scale = 111195*math.cos(math.radians(lat))
        xy = [((pt[1]-lon)*scale,(pt[0]-lat)*111195) for pt in points[start:end+1]]
        if math.hypot(*xy[anchor-start]) > 45:
            return None
        segments = []
        total = origin = 0.0
        for i,((x,y),(nx,ny)) in enumerate(zip(xy,xy[1:])):
            if i==anchor-start:
                origin = total
            dx,dy = nx-x,ny-y
            length = math.hypot(dx,dy)
            if length:
                segments.append((x,y,dx/length,dy/length,length,total))
                total += length
        if total-origin < 100:
            return None
        return cls(lat,lon,scale,tuple((x,y,ux,uy,length,s-origin) for x,y,ux,uy,length,s in segments))

    def project(self, point, max_offset=75):
        """Metres before/after the stop vertex. Closely competing branches have no assignment."""
        x,y = (point[1]-self.lon)*self.longitude_scale,(point[0]-self.lat)*111195
        matches = []
        for a,b,ux,uy,length,start in self.segments:
            dx,dy = x-a,y-b
            along = max(0,min(length,dx*ux+dy*uy))
            offset = math.hypot(dx-ux*along,dy-uy*along)
            if offset <= max_offset:
                matches.append((offset,start+along))
        if not matches:
            return None
        best,distance = min(matches)
        if any(offset < best+12 and abs(other-distance)>90 for offset,other in matches):
            return None
        return distance
