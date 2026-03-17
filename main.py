import asyncio
import base64
import codecs
import json
import logging
import re
from abc import ABC, abstractmethod
from urllib.parse import urlparse
from typing import List, Optional, Dict, Any, Union

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI
from pydantic import BaseModel
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
import uvicorn

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("HDHub4U-Engine")

app = FastAPI(title="HDHub4U Streaming Engine API")

TMDB_API_KEY = "1865f43a0549ca50d341dd9ab8b29f49"
TMDB_BASE_IMG = "https://image.tmdb.org/t/p/original"
TMDB_API_URL = "https://wild-surf-4a0d.phisher1.workers.dev"
MAIN_URL = "https://hdhub4u.rehab"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:134.0) Gecko/20100101 Firefox/134.0",
    "Cookie": "xla=s4t"
}

class ActorData(BaseModel):
    name: str
    profile: Optional[str] = None
    character: Optional[str] = None

class VideoLocal(BaseModel):
    title: Optional[str] = None
    season: Optional[int] = None
    episode: Optional[int] = None
    overview: Optional[str] = None
    thumbnail: Optional[str] = None
    released: Optional[str] = None
    rating: Optional[float] = None

class Episode(BaseModel):
    name: str
    season: Optional[int] = None
    episode: Optional[int] = None
    posterUrl: Optional[str] = None
    description: Optional[str] = None
    score: Optional[float] = None
    date: Optional[str] = None
    links: List[str]

class Subtitle(BaseModel):
    lang: str
    url: str

class ExtractorLink(BaseModel):
    source: str
    name: str
    url: str
    type: str 
    quality: int
    headers: Dict[str, str] = {}
    is_castable: bool = False
    subtitles: List[Subtitle] =[]

class LoadResponse(BaseModel):
    title: str
    url: str
    type: str
    posterUrl: Optional[str] = None
    backgroundUrl: Optional[str] = None
    logoUrl: Optional[str] = None
    year: Optional[int] = None
    plot: Optional[str] = None
    tags: List[str] = []
    actors: List[ActorData] = []
    score: Optional[float] = None
    trailer: Optional[str] = None
    imdbUrl: Optional[str] = None
    links: Optional[List[str]] = []
    episodes: Optional[List[Episode]] =[]

class SearchResult(BaseModel):
    title: str
    url: str
    type: str
    posterUrl: Optional[str] = None
    quality: Optional[str] = None

class ExtractRequest(BaseModel):
    links: List[str]


def get_search_quality(check: str) -> Optional[str]:
    if not check: return None
    s = check.lower()
    patterns =[
        (r'\b(4k|ds4k|uhd|2160p)\b', "4K"),
        (r'\b(hdts|hdcam|hdtc)\b', "HDCam"),
        (r'\b(camrip|cam[- ]?rip)\b', "CamRip"),
        (r'\b(cam)\b', "Cam"),
        (r'\b(web[- ]?dl|webrip|webdl)\b', "WebRip"),
        (r'\b(bluray|bdrip|blu[- ]?ray)\b', "BlueRay"),
        (r'\b(1440p|qhd)\b', "BlueRay"),
        (r'\b(1080p|fullhd)\b', "HD"),
        (r'\b(720p)\b', "SD"),
        (r'\b(hdrip|hdtv)\b', "HD"),
        (r'\b(dvd)\b', "DVD"),
        (r'\b(hq)\b', "HQ"),
        (r'\b(rip)\b', "CamRip")
    ]
    for pattern, quality in patterns:
        if re.search(pattern, s): return quality
    return None

def clean_title(raw: str) -> str:
    name = raw.split('(')[0].strip()
    name = re.sub(r'\s+', ' ', name).title()
    season = re.search(r'(?i)Season\s*\d+', raw)
    year = re.search(r'\b(19|20)\d{2}\b', raw)
    parts =[]
    if season: parts.append(season.group(0).capitalize())
    if year: parts.append(year.group(0))
    return f"{name} ({''.join(parts)})" if parts else name

def pen(value: str) -> str:
    return codecs.encode(value, 'rot_13')

def encode_base64(value: str) -> str:
    try: 
        return base64.b64decode(value).decode('utf-8', errors='ignore')
    except Exception as e: 
        logger.debug(f"Base64 decode error: {e}")
        return ""

def decrypt_aes(input_hex: str, key: str, iv: str) -> str:
    cipher = AES.new(key.encode('utf-8'), AES.MODE_CBC, iv.encode('utf-8'))
    decrypted_bytes = unpad(cipher.decrypt(bytes.fromhex(input_hex)), AES.block_size)
    return decrypted_bytes.decode('utf-8')

def get_base_url(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"

def extract_index_quality(string: str) -> int:
    match = re.search(r'(\d{3,4})[pP]', string)
    return int(match.group(1)) if match else 2160

def detect_stream_type(url: str) -> str:
    url_lower = url.lower()
    if ".m3u8" in url_lower: return "M3U8"
    if ".mpd" in url_lower: return "DASH"
    if ".mp4" in url_lower or ".mkv" in url_lower: return "MP4"
    return "UNKNOWN"


class BaseExtractor(ABC):
    name: str = "Base"
    domains: List[str] =[]

    @classmethod
    def can_handle(cls, url: str) -> bool:
        return any(re.search(domain, url, re.IGNORECASE) for domain in cls.domains)

    @abstractmethod
    async def extract(self, url: str, source_chain: str, client: httpx.AsyncClient) -> List[Union[str, ExtractorLink]]:
        pass

    async def safe_extract(self, url: str, source_chain: str, client: httpx.AsyncClient, retries: int = 2) -> List[Union[str, ExtractorLink]]:
        for attempt in range(retries):
            try:
                return await self.extract(url, source_chain, client)
            except Exception as e:
                logger.warning(f"Extractor {self.name} attempt {attempt + 1} failed for {url}: {e}")
                if attempt == retries - 1: 
                    return[]
                await asyncio.sleep(1)

class ExtractorEngine:
    _extractors: List[BaseExtractor] =[]

    @classmethod
    def register(cls, extractor: BaseExtractor):
        cls._extractors.append(extractor)

    @classmethod
    async def bypass_redirects(cls, url: str, client: httpx.AsyncClient) -> str:
        if "?id=" not in url: return url
        try:
            resp = await client.get(url, headers=HEADERS, follow_redirects=True)
            doc = resp.text
            regex = r"s\('o','([A-Za-z0-9+/=]+)'\)|ck\('_wp_http_\d+','([^']+)'\)"
            matches = re.findall(regex, doc)
            combined = "".join([m[0] or m[1] for m in matches if m[0] or m[1]])
            if not combined: return url
            
            b1 = base64.b64decode(combined).decode('utf-8', errors='ignore')
            b2 = base64.b64decode(b1).decode('utf-8', errors='ignore')
            decoded_str = base64.b64decode(pen(b2)).decode('utf-8', errors='ignore')
            
            json_obj = json.loads(decoded_str)
            encodedurl = encode_base64(json_obj.get("o", "")).strip()
            data = encode_base64(json_obj.get("data", "")).strip()
            wphttp1 = json_obj.get("blog_url", "").strip()
            
            if wphttp1 and data:
                try:
                    r2 = await client.get(f"{wphttp1}?re={data}", headers=HEADERS)
                    soup = BeautifulSoup(r2.text, 'html.parser')
                    body = soup.find('body')
                    if body: return body.get_text(strip=True)
                except Exception as inner_e: 
                    logger.debug(f"WPHTTP bypass secondary fetch failed: {inner_e}")
            return encodedurl if encodedurl else url
        except Exception as e: 
            logger.error(f"Redirect bypass failed for {url}: {e}")
            return url

    @classmethod
    async def resolve(cls, url: str, source_chain: str, client: httpx.AsyncClient, depth: int = 0) -> List[ExtractorLink]:
        if depth > 5: 
            logger.warning(f"Max extraction depth reached for {url}")
            return[] 
        
        real_url = await cls.bypass_redirects(url, client)
        
        extractor = next((e for e in cls._extractors if e.can_handle(real_url)), None)
        
        if not extractor:
            stream_type = detect_stream_type(real_url)
            logger.info(f"No extractor found for {real_url}, falling back to Direct Stream")
            return[ExtractorLink(
                source=f"{source_chain} -> Direct",
                name="Direct Stream",
                url=real_url,
                type=stream_type,
                quality=0,
                is_castable=(stream_type == "MP4")
            )]

        logger.info(f"Routing {real_url} -> {extractor.name}")
        results = await extractor.safe_extract(real_url, source_chain, client)
        
        final_links = []
        nested_tasks =[]

        for res in results:
            if isinstance(res, str):
                nested_tasks.append(cls.resolve(res, f"{source_chain} -> {extractor.name}", client, depth + 1))
            elif isinstance(res, ExtractorLink):
                final_links.append(res)

        if nested_tasks:
            nested_results = await asyncio.gather(*nested_tasks, return_exceptions=True)
            for n_res in nested_results:
                if isinstance(n_res, list): 
                    final_links.extend(n_res)
                elif isinstance(n_res, Exception):
                    logger.error(f"Nested extraction failed: {n_res}")

        return final_links


class VidstackExtractor(BaseExtractor):
    name = "Vidstack"
    domains =[r"vidstack\.io", r"hdstream4u\.", r"hubstream\."]

    async def extract(self, url: str, source_chain: str, client: httpx.AsyncClient) -> List[Union[str, ExtractorLink]]:
        hash_val = url.split("#")[-1].split("/")[-1]
        baseurl = get_base_url(url)
        api_url = f"{baseurl}/api/v1/video?id={hash_val}"
        
        resp = await client.get(api_url, headers=HEADERS)
        encoded = resp.text.strip()
        key, iv_list = "kiemtienmua911ca",["1234567890oiuytr", "0123456789abcdef"]
        
        decrypted_text = None
        for iv in iv_list:
            try:
                decrypted_text = decrypt_aes(encoded, key, iv)
                if decrypted_text: break
            except Exception:
                continue
        
        if not decrypted_text: 
            logger.error(f"Vidstack decryption failed for {url}")
            return[]
            
        m3u8_match = re.search(r'"source":"(.*?)"', decrypted_text)
        m3u8 = m3u8_match.group(1).replace("\\/", "/").replace("https", "http") if m3u8_match else ""
        
        subtitles =[]
        subtitle_section = re.search(r'"subtitle":\{(.*?)\}', decrypted_text)
        if subtitle_section:
            for match in re.finditer(r'"([^"]+)":\s*"([^"]+)"', subtitle_section.group(1)):
                raw_path = match.group(2).split("#")[0]
                if raw_path:
                    subtitles.append(Subtitle(lang=match.group(1), url=f"{baseurl}{raw_path.replace('\\/', '/')}"))
                    
        return[ExtractorLink(
            source=f"{source_chain} -> {self.name}",
            name=self.name,
            url=m3u8,
            type="M3U8",
            quality=0,
            headers={"referer": url, "Origin": baseurl},
            is_castable=True,
            subtitles=subtitles
        )]

class HubCloudExtractor(BaseExtractor):
    name = "HubCloud"
    domains = [r"hubcloud\."]

    async def extract(self, url: str, source_chain: str, client: httpx.AsyncClient) -> List[Union[str, ExtractorLink]]:
        uri = urlparse(url)
        base_url = f"{uri.scheme}://{uri.netloc}"
        
        if "hubcloud.php" not in url:
            resp1 = await client.get(url, headers=HEADERS)
            soup = BeautifulSoup(resp1.text, 'html.parser')
            raw = (soup.select_one("#download") or {}).get("href", "")
            href = raw if raw.startswith("http") else f"{base_url.rstrip('/')}/{raw.lstrip('/')}"
        else:
            href = url
            
        if not href: return[]
            
        doc_resp = await client.get(href, headers=HEADERS)
        doc = BeautifulSoup(doc_resp.text, 'html.parser')
        
        header = (doc.select_one("div.card-header") or BeautifulSoup("<div></div>", "html.parser")).text.strip()
        quality = extract_index_quality(header)
        
        links =[]
        for element in doc.select("a.btn"):
            link, label = element.get("href", ""), element.text.lower()
            
            if "buzzserver" in label:
                try:
                    bz_resp = await client.get(f"{link}/download", headers={"Referer": link}, follow_redirects=False)
                    dlink = bz_resp.headers.get("hx-redirect") or bz_resp.headers.get("HX-Redirect", "")
                    if dlink: links.append(dlink)
                except Exception as e:
                    logger.debug(f"Buzzserver fetch failed: {e}")
            elif any(x in label for x in["pixeldra", "pixelserver", "pixel server", "pixeldrain"]):
                final_url = link if "download" in link else f"{get_base_url(link)}/api/file/{link.split('/')[-1]}?download"
                links.append(final_url)
            else:
                links.append(ExtractorLink(
                    source=f"{source_chain} -> {self.name}",
                    name=element.text.strip(),
                    url=link,
                    type=detect_stream_type(link),
                    quality=quality,
                    headers={"referer": href}
                ))
        return links

class HbLinksExtractor(BaseExtractor):
    name = "HbLinks"
    domains = [r"hblinks\."]

    async def extract(self, url: str, source_chain: str, client: httpx.AsyncClient) -> List[Union[str, ExtractorLink]]:
        resp = await client.get(url, headers=HEADERS)
        soup = BeautifulSoup(resp.text, 'html.parser')
        return[a.get("href") for a in soup.select("h3 a, h5 a, div.entry-content p a") if a.get("href")]

class HubDriveExtractor(BaseExtractor):
    name = "HubDrive"
    domains = [r"hubdrive\."]

    async def extract(self, url: str, source_chain: str, client: httpx.AsyncClient) -> List[Union[str, ExtractorLink]]:
        resp = await client.get(url, timeout=5.0)
        btn = BeautifulSoup(resp.text, 'html.parser').select_one(".btn.btn-primary.btn-user.btn-success1.m-1")
        return [btn.get("href")] if btn and btn.get("href") else[]

class HubCdnExtractor(BaseExtractor):
    name = "HubCDN"
    domains = [r"hubcdn\."]

    async def extract(self, url: str, source_chain: str, client: httpx.AsyncClient) -> List[Union[str, ExtractorLink]]:
        resp = await client.get(url, headers=HEADERS)
        match = re.search(r'reurl\s*=\s*"([^"]+)"', resp.text)
        if match:
            try:
                decoded_url = base64.b64decode(match.group(1).split("?r=")[-1]).decode('utf-8').split("link=")[-1]
                return[ExtractorLink(
                    source=f"{source_chain} -> {self.name}",
                    name=self.name,
                    url=decoded_url,
                    type="M3U8",
                    quality=0,
                    is_castable=True
                )]
            except Exception as e:
                logger.error(f"HubCDN decoding failed: {e}")
        return[]

class PixelDrainExtractor(BaseExtractor):
    name = "PixelDrain"
    domains =[r"pixeldrain\.com", r"pixeldrain\.dev"]

    async def extract(self, url: str, source_chain: str, client: httpx.AsyncClient) -> List[Union[str, ExtractorLink]]:
        return[ExtractorLink(
            source=f"{source_chain} -> {self.name}",
            name=self.name,
            url=url,
            type="MP4",
            quality=0,
            is_castable=True
        )]


ExtractorEngine.register(VidstackExtractor())
ExtractorEngine.register(HubCloudExtractor())
ExtractorEngine.register(HbLinksExtractor())
ExtractorEngine.register(HubDriveExtractor())
ExtractorEngine.register(HubCdnExtractor())
ExtractorEngine.register(PixelDrainExtractor())


@app.get("/home", response_model=List[SearchResult])
async def get_home(page: int = 1):
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(f"{MAIN_URL}/page/{page}/", headers=HEADERS, follow_redirects=True)
            soup = BeautifulSoup(resp.text, 'html.parser')
            results =[]
            for post in soup.select(".recent-movies > li.thumb"):
                title_p = post.select_one("figcaption:nth-child(2) > a:nth-child(1) > p:nth-child(1)")
                if not title_p: continue
                
                title_text = title_p.text.strip()
                a_tag = post.select_one("figure:nth-child(1) > a:nth-child(2)")
                img_tag = post.select_one("figure:nth-child(1) > img:nth-child(1)")
                
                results.append(SearchResult(
                    title=clean_title(title_text),
                    url=a_tag.get("href", "") if a_tag else "",
                    type="Movie",
                    posterUrl=img_tag.get("src", "") if img_tag else "",
                    quality=get_search_quality(title_text)
                ))
            return results
        except Exception as e:
            logger.error(f"Failed to fetch home page {page}: {e}")
            return[]

@app.get("/search", response_model=List[SearchResult])
async def search(query: str, page: int = 1):
    async with httpx.AsyncClient() as client:
        try:
            url = (
                f"https://search.pingora.fyi/collections/post/documents/search"
                f"?q={query}&query_by=post_title,category&query_by_weights=4,2"
                f"&sort_by=sort_by_date:desc&limit=15&highlight_fields=none&use_cache=true&page={page}"
            )
            resp = await client.get(url, headers=HEADERS)
            return[SearchResult(
                title=doc.get("post_title", ""),
                url=doc.get("permalink", ""),
                type="Movie",
                posterUrl=doc.get("post_thumbnail", "")
            ) for hit in resp.json().get("hits", []) if (doc := hit.get("document", {}))]
        except Exception as e:
            logger.error(f"Search failed for query '{query}': {e}")
            return[]

@app.get("/details", response_model=LoadResponse)
async def get_details(url: str):
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=HEADERS)
        doc = BeautifulSoup(resp.text, 'html.parser')
        
        title = (doc.select_one(".page-body h2[data-ved], h2[data-ved]") or BeautifulSoup("<h2></h2>", "html.parser")).text.strip()
        season_match = re.search(r'(?i)\bSeason\s*(\d+)\b', title)
        season_num = int(season_match.group(1)) if season_match else None
        
        image = (doc.select_one("meta[property='og:image']") or {}).get("content", "")
        plot = (doc.select_one(".kno-rdesc .kno-rdesc") or BeautifulSoup("<div></div>", "html.parser")).text.strip()
        tags =[em.text.strip() for em in doc.select(".page-meta em")]
        poster = (doc.select_one("main.page-body img.aligncenter") or {}).get("src", "")
        trailer = (doc.select_one(".responsive-embed-container > iframe:nth-child(1)") or {}).get("src", "").replace("/embed/", "/watch?v=")
        
        typeraw = (doc.select_one("h1.page-title span") or BeautifulSoup("<span></span>", "html.parser")).text.strip()
        is_movie = "movie" in typeraw.lower()
        tv_type = "Movie" if is_movie else "TvSeries"
        
        imdb_url, tmdb_id_resolved = "", ""
        if tmdb_elem := doc.select_one("div span a[href*='themoviedb.org']"):
            tmdb_id_resolved = tmdb_elem.get("href", "").split("/")[-1].split("-")[0].split("?")[0]
        
        if imdb_elem := doc.select_one("div span a[href*='imdb.com']"):
            imdb_url = imdb_elem.get("href", "")
            if not tmdb_id_resolved and "title/" in imdb_url:
                try:
                    imdb_id_only = imdb_url.split('title/')[1].split('/')[0]
                    find_resp = await client.get(f"{TMDB_API_URL}/find/{imdb_id_only}?api_key={TMDB_API_KEY}&external_source=imdb_id")
                    res = find_resp.json().get("movie_results" if is_movie else "tv_results", [])
                    if res: tmdb_id_resolved = str(res[0].get("id"))
                except Exception as e: 
                    logger.debug(f"IMDB to TMDB resolution failed: {e}")
        elif tmdb_id_resolved:
            try:
                ext_resp = await client.get(f"{TMDB_API_URL}/{'tv' if not is_movie else 'movie'}/{tmdb_id_resolved}/external_ids?api_key={TMDB_API_KEY}")
                imdb_url = ext_resp.json().get("imdb_id", "")
            except Exception as e: 
                logger.debug(f"TMDB to IMDB resolution failed: {e}")

        actors_data, genres_list, year, background, score, logo, episodes_data, videos_list = [],[], "", image, None, None, [],[]

        if tmdb_id_resolved:
            try:
                details_resp = await client.get(f"{TMDB_API_URL}/{'tv' if not is_movie else 'movie'}/{tmdb_id_resolved}?api_key={TMDB_API_KEY}&append_to_response=credits,external_ids")
                details = details_resp.json()
                
                meta_name = details.get("name") or details.get("title") or title
                if season_num and f"Season {season_num}" not in meta_name: meta_name = f"{meta_name} (Season {season_num})"
                title, plot = meta_name, details.get("overview") or plot
                year_raw = details.get("release_date") or details.get("first_air_date", "")
                year = year_raw[:4] if year_raw else ""
                score = details.get("vote_average")
                if bg_path := details.get("backdrop_path"): background = TMDB_BASE_IMG + bg_path
                if imdb_id_val := details.get("external_ids", {}).get("imdb_id"): logo = f"https://live.metahub.space/logo/medium/{imdb_id_val}/img"
                    
                actors_data =[ActorData(name=c.get("name") or c.get("original_name", ""), profile=TMDB_BASE_IMG + c.get("profile_path") if c.get("profile_path") else None, character=c.get("character")) for c in details.get("credits", {}).get("cast", [])]
                
                for g in details.get("genres",[]):
                    if g_name := g.get("name"):
                        genres_list.append(g_name)
                        if g_name not in tags: tags.append(g_name)
                        
                if not is_movie and season_num is not None:
                    season_json = (await client.get(f"{TMDB_API_URL}/tv/{tmdb_id_resolved}/season/{season_num}?api_key={TMDB_API_KEY}")).json()
                    videos_list =[VideoLocal(title=ep.get("name"), season=season_num, episode=ep.get("episode_number"), overview=ep.get("overview"), thumbnail=TMDB_BASE_IMG + ep.get("still_path") if ep.get("still_path") else None, released=ep.get("air_date"), rating=ep.get("vote_average")) for ep in season_json.get("episodes", [])]
            except Exception as e: 
                logger.warning(f"TMDB details fetch failed: {e}")

        if is_movie:
            movie_links =[a.get("href") for a in doc.select("h3 a, h4 a") if re.search(r'(480|720|1080|2160|4K)', a.text)]
            movie_links +=[href for a in doc.select(".page-body > div a") if (href := a.get("href")) and re.search(r'https://(.*\.)?(hdstream4u|hubstream)\..*', href) and href not in movie_links]
            return LoadResponse(title=title, url=url, type=tv_type, posterUrl=poster, backgroundUrl=background, logoUrl=logo, year=int(year) if year.isdigit() else None, plot=plot, tags=tags, actors=actors_data, score=score, trailer=trailer, imdbUrl=imdb_url, links=list(set(movie_links)))
        else:
            ep_links_map = {}
            for element in doc.select("h3, h4"):
                ep_match = re.search(r'EPiSODE\s*(\d+)', element.text, re.IGNORECASE)
                ep_num = int(ep_match.group(1)) if ep_match else None
                base_links =[a.get("href") for a in element.select("a[href]") if a.get("href")]
                all_ep_links = set()
                
                if any(re.search(r'1080|720|4K|2160', a.text, re.IGNORECASE) for a in element.select("a")):
                    for link in base_links:
                        try:
                            resolved = await ExtractorEngine.bypass_redirects(link.strip(), client)
                            ep_doc = BeautifulSoup((await client.get(resolved, headers=HEADERS)).text, 'html.parser')
                            for ep_a in ep_doc.select("h5 a"):
                                if (l := ep_a.get("href")) and (m := re.search(r'Episode\s*(\d+)', ep_a.text, re.IGNORECASE)):
                                    ep_links_map.setdefault(int(m.group(1)),[]).append(l)
                        except Exception as e:
                            logger.debug(f"Direct link resolution failed for episode: {e}")
                elif ep_num is not None:
                    if element.name == "h4":
                        nxt = element.find_next_sibling()
                        while nxt and nxt.name != "hr":
                            all_ep_links.update([a.get("href") for a in nxt.select("a[href]") if a.get("href")])
                            nxt = nxt.find_next_sibling()
                    all_ep_links.update(base_links)
                    if all_ep_links: ep_links_map.setdefault(ep_num,[]).extend(list(all_ep_links))
            
            for ep_num, links in ep_links_map.items():
                info = next((v for v in videos_list if v.season == season_num and v.episode == ep_num), None)
                episodes_data.append(Episode(name=info.title if info else f"Episode {ep_num}", season=season_num, episode=ep_num, posterUrl=info.thumbnail if info else None, description=info.overview if info else None, score=info.rating if info else None, date=info.released if info else None, links=list(set(links))))
                
            return LoadResponse(title=title, url=url, type=tv_type, posterUrl=poster, backgroundUrl=background, logoUrl=logo, year=int(year) if year.isdigit() else None, plot=plot, tags=tags, actors=actors_data, score=score, trailer=trailer, imdbUrl=imdb_url, episodes=episodes_data)

@app.post("/extract")
async def extract_links(req: ExtractRequest):
    async with httpx.AsyncClient() as client:
        tasks = [ExtractorEngine.resolve(link, "Initial", client) for link in req.links]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        flattened_streams =[]
        for res in results:
            if isinstance(res, list):
                flattened_streams.extend(res)
            elif isinstance(res, Exception):
                logger.error(f"Top-level extraction failed: {res}")
                
        return {"streams": flattened_streams}

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000)
