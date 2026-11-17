from pathlib import Path
import tempfile
import unittest
from PIL import Image,ImageDraw,ImageFilter,ImageOps

from talkturn.reply_photo import match_photos


# Internally generated test art and its Kakao quote thumbnail; AGPL-3.0-only.
FIXTURES=Path(__file__).parent/'fixtures'
NATIVE_QUOTE=FIXTURES/'kakao-quote-thumbnail.png'


class ReplyPhotoTest(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory(); self.addCleanup(self.folder.cleanup)
        self.root=Path(self.folder.name)
        self.source=Image.new('RGB',(320,160),'#24364b')
        draw=ImageDraw.Draw(self.source)
        draw.rectangle((95,12,135,70),fill='#e98b21')
        draw.ellipse((140,30,220,115),fill='#72bdcf')
        draw.rectangle((110,125,205,143),fill='white')
        self.original=self.root/'original.png'; self.source.save(self.original)
        self.quote=self.root/'quote.png'
        ImageOps.fit(self.source,(35,35),Image.Resampling.LANCZOS).filter(ImageFilter.GaussianBlur(.2)).save(self.quote)
        self.addCleanup(self.source.close)

    def test_center_crop_matches_original_and_stored_thumbnail_not_other_image(self):
        small=self.root/'stored.png'
        self.source.resize((240,120),Image.Resampling.LANCZOS).save(small)
        other=self.root/'other.png'
        ImageOps.mirror(self.source).save(other)
        self.assertEqual(match_photos(self.quote,[other,self.original,small]),[str(self.original),str(small)])

    def test_identical_and_visually_indistinguishable_centers_stay_ambiguous(self):
        duplicate=self.root/'duplicate.png'; self.source.save(duplicate)
        different=self.root/'different-edges.png'
        altered=self.source.copy(); ImageDraw.Draw(altered).rectangle((0,0,60,159),fill='red'); altered.save(different); altered.close()
        self.assertEqual(match_photos(self.quote,[self.original,duplicate,different,self.original]),
                         [str(self.original),str(duplicate),str(different)])

    def test_flat_and_nearly_uniform_quotes_fail_closed(self):
        for noise in (False,True):
            image=Image.new('RGB',(35,35),(40,50,60))
            if noise: ImageDraw.Draw(image).rectangle((15,15,18,18),fill=(45,55,65))
            path=self.root/f'flat-{noise}.png'; image.save(path); image.close()
            self.assertEqual(match_photos(path,[path,self.original]),[])

    def test_invalid_quote_and_reference_fail_closed(self):
        broken=self.root/'broken.png'; broken.write_bytes(b'not an image')
        self.assertEqual(match_photos(broken,[self.original]),[])
        self.assertEqual(match_photos(self.quote,[broken,self.root/'missing.png']),[])
        self.assertEqual(match_photos(self.original,[self.original]),[])

    def test_text_page_shifted_a_pixel_by_another_scaler_matches_only_its_own_page(self):
        import random
        def page(seed):
            random.seed(seed)
            image=Image.new('RGB',(337,480),(250,250,248)); draw=ImageDraw.Draw(image)
            for y in range(14,470,9):
                x=14; end=random.randint(120,322)
                while x<end:
                    width=random.randint(3,9); draw.rectangle((x,y,x+width,y+4),fill=(40,40,40)); x+=width+random.randint(2,4)
            return image
        own=self.root/'own.png'; other=self.root/'other.png'; quote=self.root/'page-quote.png'
        page(1).save(own); page(2).save(other)
        moved=Image.new('RGB',(337,480),(250,250,248)); moved.paste(page(1),(1,1))
        ImageOps.fit(moved,(66,66),Image.Resampling.BILINEAR).save(quote)
        self.assertEqual(match_photos(quote,[other,own]),[str(own)])

    def test_actual_kakao_extracted_quote_matches_original(self):
        original=FIXTURES/'kakao-quote-original.png'
        self.assertEqual(match_photos(NATIVE_QUOTE,[original]),[str(original)])


if __name__=='__main__':
    unittest.main()
