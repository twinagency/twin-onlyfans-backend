"""Legacy matching used ONLY to retain prior manual removals during migration."""
import json,re
from vault_ai_effective import effective_value
FOLDERS = {'selfies': ('Selfies', {'selfie', 'selfies', 'mirror selfie', 'handheld selfie', 'sfw selfie'}), 'swimwear': ('Bikini / swimwear', {'bikini', 'swimsuit', 'swimwear', 'bathing suit'}), 'lingerie': ('Lingerie', {'lingerie', 'lingerie on', 'bra', 'panties', 'underwear', 'thong'}), 'casual': ('Casual / everyday', {'casual', 'everyday', 'casual outfit', 'casual clothes', 'streetwear'}), 'fitness': ('Fitness', {'fitness', 'workout', 'gym', 'exercise', 'activewear', 'sports bra'}), 'outdoors': ('Outdoors', {'outdoor', 'outdoors', 'outside', 'garden'}), 'bedroom': ('Bedroom', {'bedroom', 'bed'}), 'bathroom': ('Bathroom / shower', {'bathroom', 'shower', 'bath', 'shower bath'}), 'studio': ('Studio', {'studio', 'photoshoot', 'photo shoot'}), 'beach': ('Beach / pool', {'beach', 'pool', 'poolside', 'swimming pool'}), 'cosplay': ('Outfits / cosplay', {'cosplay', 'costume', 'outfit cosplay'}), 'talking': ('Talking videos', {'talking head', 'talking to camera'}), 'dance': ('Dance / twerk', {'dance', 'dancing', 'twerk', 'ass twerk', 'ass shaking'}), 'solo_tease': ('Solo tease', {'solo tease'}), 'solo_fingering': ('Solo fingering', {'solo fingering'}), 'solo_toy': ('Solo toys', {'solo toy'}), 'nude_stills': ('Nude photos', {'nude stills'}), 'boobs': ('Boobs', {'boobs'}), 'blowjob': ('Blowjob', {'blowjob'}), 'partner': ('With partner', {'sex with partner'}), 'feet': ('Feet', {'feet'})}

def _normal(value):
    return re.sub('\\s+', ' ', str(value).lower().replace('_', ' ').replace('-', ' ')).strip()

def _tags(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = re.split('[,\\n]', value)
    return {_normal(t) for t in value if isinstance(t, str)} if isinstance(value, (list, tuple)) else set()

def categories(item):
    """Conservative, overlapping categories from operator-effective metadata."""
    tags = _tags(effective_value(item, 'tags', default=[]))
    tags |= _tags(effective_value(item, 'clothing_items', default=[]))
    for key in ('camera', 'primary_folder', 'clothing_state', 'setting'):
        value = effective_value(item, key)
        if isinstance(value, str):
            tags.add(_normal(value))
    result = {key for key, (_, terms) in FOLDERS.items() if tags & terms}
    desc = effective_value(item, 'description') or effective_value(item, 'video_description') or ''
    if isinstance(desc, str):
        for sentence in re.split('[.!?;\\n]', _normal(desc)):
            if re.search('\\b(no|not|without|neither|removed|removing|off)\\b', sentence):
                continue
            for key, (_, terms) in FOLDERS.items():
                if any((re.search('(?<!\\w)' + re.escape(term) + '(?!\\w)', sentence) for term in terms)):
                    result.add(key)
    if _normal(effective_value(item, 'clothing_state') or '') == 'fully nude':
        result -= {'swimwear', 'lingerie', 'casual'}
    return sorted(result) or ['uncategorised']
