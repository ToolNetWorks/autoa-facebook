"""Conservative article evidence, independent of submit notices and business state."""
from __future__ import annotations

from io import BytesIO
from pathlib import Path
from PIL import Image, ImageChops, ImageStat, ImageOps

from services.facebook_selectors import current_registry

# Scope every item to its nearest article: comments/shared posts cannot provide
# caption, identity, photo or permalink evidence for the surrounding post.
CANDIDATES = r"""(articles, args) => {
 const norm=s=>(s||'').replace(/\s+/g,' ').trim();
 const own=(a,selector)=>[...a.querySelectorAll(selector)].filter(n=>n.closest(args.selectors.article_root)===a);
 const group=new URL(args.group).pathname.match(/^\/groups\/([^/]+)/)?.[1];
 const canonical=h=>{try {const u=new URL(h);return u.pathname.replace(/\/$/,'');}catch{return '';}};
 const old=new Set(args.exclude.map(canonical));
 if(!group||!norm(args.body))return [];
 return articles.flatMap((a,index)=>{
  if(!a.getClientRects().length)return [];
  const messages=own(a,args.selectors.message)
    .filter(n=>n.getClientRects().length);
  if(!messages.some(n=>norm(n.innerText)===norm(args.body)))return [];
  const authors=own(a,args.selectors.authors).map(n=>norm(n.innerText)).filter(Boolean);
  const images=own(a,args.selectors.images).filter(img=>{
    if(img.closest(args.selectors.authors))return false;
    const link=img.closest(args.selectors.links);
    const photo=link && /\/(photo|photos)(\.php|\/|\?)/.test(link.href);
    return photo && img.complete && img.naturalWidth>=160 && img.naturalHeight>=120 &&
      img.getClientRects().length && !/avatar|profile picture|ảnh đại diện/i.test(img.alt||'');
  }).map(img=>[...a.querySelectorAll(args.selectors.images)].indexOf(img));
  const links=own(a,args.selectors.links).flatMap(link=>{
   const u=new URL(link.href);const m=u.pathname.match(/^\/groups\/([^/]+)\/(posts|permalink|pending_posts)\/([^/]+)\/?$/);
   if(u.protocol!=='https:'||u.username||u.password||!['www.facebook.com','web.facebook.com','m.facebook.com','facebook.com'].includes(u.hostname)||!m||m[1]!==group||old.has(canonical(u.href)))return [];
   const stamp=link.querySelector(args.selectors.timestamp)?.getAttribute('datetime') || link.getAttribute('data-utime');
   let fresh=false;
   if(stamp){const t=/^\d+$/.test(stamp)?Number(stamp)*1000:Date.parse(stamp);fresh=t>=args.since-60000&&t<=Date.now()+60000;}
   else fresh=/^(just now|now|vừa xong|vừa mới)$/i.test(norm(link.innerText));
   return fresh?[u.origin+u.pathname]:[];
  });
  return [{index,images,authors,links:[...new Set(links)]}];
 });
}"""


def same_photo(expected: Path, rendered: bytes) -> bool:
    """Compare decoded pixels, not CDN URLs. Low-texture images still compare RGB."""
    try:
        with Image.open(expected) as source, Image.open(BytesIO(rendered)) as shot:
            source = ImageOps.exif_transpose(source).convert('RGB')
            shot = shot.convert('RGB')
            if abs((source.width/source.height)/(shot.width/shot.height)-1) > .05:
                return False
            source = source.resize((32, 32), Image.Resampling.LANCZOS)
            shot = shot.resize((32, 32), Image.Resampling.LANCZOS)
            difference = ImageStat.Stat(ImageChops.difference(source, shot))
            return max(difference.mean) <= 12 and max(difference.rms) <= 24
    except (OSError, ValueError):
        return False


async def verify_post(page, body: str, *, expected_group: str, expected_image: Path | str | None,
                      submitted_at: float | None, expected_identity: str = '', exclude=None) -> dict:
    result = {'permalink': '', 'reason': 'insufficient_article_evidence'}
    if not expected_image or submitted_at is None or not expected_group:
        return result
    registry = current_registry()
    articles = registry.all(page, 'articles')
    candidates = await articles.evaluate_all(CANDIDATES, {
        'selectors': {key: registry.css(key) for key in ('article_root','message','authors','images','links','timestamp')},
        'body':body, 'group':expected_group, 'exclude':exclude or [], 'since':submitted_at*1000})
    if len(candidates)>20:
        return {**result, 'reason':'too_many_candidates'}
    matches=[]
    for candidate in candidates:
        if len(candidate['links'])!=1 or len(candidate['images'])!=1:
            continue
        authors=candidate['authors']
        if expected_identity:
            allowed={expected_identity.casefold().strip()}
            if allowed & {'anonymous','anonymous member','thành viên ẩn danh','anonymous participant','người tham gia ẩn danh'}:
                allowed.update({'anonymous member','thành viên ẩn danh','anonymous participant','người tham gia ẩn danh'})
            if not authors or not any(author.casefold().strip() in allowed for author in authors):
                continue
        photo=articles.nth(candidate['index']).locator(registry.css('images')).nth(candidate['images'][0])
        try:
            rendered=await photo.screenshot(timeout=3000, animations='disabled')
        except Exception:
            continue
        if same_photo(Path(expected_image),rendered):
            matches.append(candidate['links'][0])
    if len(matches)!=1:
        return {**result,'reason':'ambiguous_target' if len(matches)>1 else result['reason']}
    return {'permalink':matches[0], 'reason':'caption_group_photo_time_identity_matched'}
