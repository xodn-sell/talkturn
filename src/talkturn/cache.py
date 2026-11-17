"""Trim only TalkTurn-generated, no-longer-needed images. Preserve source files."""
import hashlib
from pathlib import Path
import re
import time
from .runtime import ROOT,read_json,locked


def trim_generated_images():
    removed=0; count=0
    with locked('cache.lock',timeout=2):
        retained={str(Path(path).resolve()) for row in read_json('result-origins.json',[]) for path in row.get('images',[])}
        verified={hashlib.sha256(key.encode()).hexdigest()[:20] for key in read_json('native-delivered.json',[])}
        for name,pattern in (('rendered',r'([0-9a-f]{20})-[0-9]+\.png'),('sent-images',r'[0-9a-f]{64}\.png')):
            folder=(ROOT/name).resolve()
            for path in folder.glob('*.png'):
                match=re.fullmatch(pattern,path.name)
                if not match or path.is_symlink() or path.resolve().parent!=folder or str(path.resolve()) in retained:
                    continue
                info=path.stat(); age=time.time()-info.st_mtime
                discard=(match[1] in verified or age>7*86400) if name=='rendered' else age>86400
                if discard:
                    path.unlink(); removed+=info.st_size; count+=1
        # Older versions left an uncompressed capture after saving the cropped PNG.
        bmp=ROOT/'reply-view.bmp'; png=ROOT/'reply-view.png'
        if bmp.is_file() and not bmp.is_symlink() and png.is_file() and png.stat().st_mtime>=bmp.stat().st_mtime:
            info=bmp.stat()
            if time.time()-info.st_mtime>86400:
                bmp.unlink(); removed+=info.st_size; count+=1
    return {'removed_files':count,'freed_bytes':removed}
