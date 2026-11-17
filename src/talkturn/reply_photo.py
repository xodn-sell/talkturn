"""Conservative local matching of Kakao's center-square photo quote thumbnails.

A tiny crop cannot prove full-image identity: different photos can share its
visible center, and compression may erase distinguishing details. Return every
plausible candidate, never a nearest-neighbor guess. The caller must require one
unique original session across the returned paths. Low-detail quotes fail closed.
"""
import math
from pathlib import Path

from PIL import Image,ImageChops,ImageFilter,ImageOps,ImageStat,UnidentifiedImageError


def _read(path):
    with Image.open(path) as image:
        if image.width*image.height>20_000_000:
            raise ValueError('Reference image is too large')
        return image.convert('RGB')


def _detailed(image):
    gray=image.convert('L')
    values=gray.tobytes()
    median=sorted(values)[len(values)//2]
    return (ImageStat.Stat(gray).stddev[0]>=8
            and sum(abs(value-median)>20 for value in values)>=max(12,len(values)*.025))


def _metrics(quote,reference):
    raw=sum(ImageStat.Stat(ImageChops.difference(quote,reference)).mean)/3
    # One thumbnail pixel of smoothing tolerates screenshot compression/antialiasing.
    a=quote.filter(ImageFilter.GaussianBlur(1))
    b=reference.filter(ImageFilter.GaussianBlur(1))
    blurred=sum(ImageStat.Stat(ImageChops.difference(a,b)).mean)/3
    x=a.convert('L').tobytes(); y=b.convert('L').tobytes()
    mx=sum(x)/len(x); my=sum(y)/len(y)
    variance=sum((v-mx)**2 for v in x)*sum((v-my)**2 for v in y)
    correlation=sum((u-mx)*(v-my) for u,v in zip(x,y))/math.sqrt(variance) if variance else 0
    return raw,blurred,correlation


def match_photos(quote_thumbnail_path,candidate_paths):
    """Return plausible original paths only when at least one match is strong.

    Accept a tightly cropped, roughly square 16–128 px quote, not a whole bubble.
    Candidate files are read explicitly; no folder search or model call is used.
    """
    try:
        quote=_read(quote_thumbnail_path)
    except (OSError,ValueError,UnidentifiedImageError,Image.DecompressionBombError):
        return []
    width,height=quote.size
    if min(width,height)<16 or max(width,height)>128 or abs(width-height)>2:
        quote.close()
        return []
    inset=max(1,round(min(width,height)*.06))
    box=(inset,inset,width-inset,height-inset)
    core=quote.crop(box)
    quote.close()
    if not _detailed(core):
        core.close()
        return []
    plausible=[]; strong=False
    for path in dict.fromkeys(str(path) for path in candidate_paths):
        try:
            with _read(Path(path)) as source:
                fitted=ImageOps.fit(source,(width,height),Image.Resampling.LANCZOS)
                reference=fitted.crop(box)
                fitted.close()
            with reference:
                if not _detailed(reference):
                    continue
                raw,blurred,correlation=_metrics(core,reference)
            # A page of thin text shifts by a fraction of a pixel between Kakao's scaler and ours,
            # which inflates the unblurred difference of the right photo (13.6 seen) into the range
            # of wrong ones (17-23). The blurred difference and correlation separate them (.95 vs .56).
            strong=strong or (blurred<=6 and correlation>=.94)
            # Keep weaker competing matches too; caller rejects cross-session ambiguity.
            if blurred<=10 and correlation>=.90:
                plausible.append(path)
        except (OSError,ValueError,UnidentifiedImageError,Image.DecompressionBombError):
            continue
    core.close()
    return plausible if strong else []
