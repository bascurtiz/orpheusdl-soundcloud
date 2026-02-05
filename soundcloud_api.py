from utils.utils import create_requests_session


class SoundCloudWebAPI:
    def __init__(self, access_token, exception):
        self.api_base = 'https://api-v2.soundcloud.com/'
        self.access_token = access_token
        self.exception = exception
        self.s = create_requests_session()


    def _headers(self):
        return {
            'Authorization': f'OAuth {self.access_token}',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/80.0.3987.132 Safari/537.36'
        }

    def _get(self, url, params=None):
        params = params or {}
        headers = self._headers()
        if url.startswith('http://') or url.startswith('https://'):
            r = self.s.get(url, headers=headers)
        else:
            r = self.s.get(f'{self.api_base}{url}', params=params, headers=headers)
        if r.status_code not in [200, 201, 202]:
            if r.status_code == 403:
                raise self.exception("This track is not available (e.g. restricted in your country or disabled for API access).")
            raise self.exception(f'{r.status_code!s}: {r.text}')
        return r.json()

    def _get_collection_paginated(self, url, params=None, max_pages=50):
        """Fetch a paginated collection; handle both list response and dict with collection/next_href. Follow next_href until no more."""
        params = params or {}
        all_items = []
        page_url = url
        page_params = params
        for _ in range(max_pages):
            if page_url.startswith('http://') or page_url.startswith('https://'):
                resp = self._get(page_url)
                page_url = None
                page_params = None
            else:
                resp = self._get(page_url, page_params)
                page_url = None
            if isinstance(resp, list):
                collection = resp
                next_href = None
            elif isinstance(resp, dict):
                collection = resp.get('collection', [])
                next_href = resp.get('next_href')
            else:
                collection = []
                next_href = None
            for i in collection:
                if isinstance(i, dict) and 'id' in i:
                    all_items.append(i)
            if not next_href:
                break
            page_url = next_href
        return all_items


    def get_track_download(self, track_id):
        return self._get(f'tracks/{track_id}/download')['redirectUri']
    

    def get_track_stream_link(self, file_url, access_token): # Why does strip/lstrip not work here...?
        if not file_url or not isinstance(file_url, str):
            raise self.exception("Stream URL is missing. This track may not be available for streaming or download in your region.")
        if 'https://api-v2.soundcloud.com/' not in file_url:
            raise self.exception("Invalid stream URL. This track may not be available.")
        return self._get(file_url.split('https://api-v2.soundcloud.com/')[1], {'track_authorization': access_token})['url']

    def get_preview_stream_url(self, track_id, track_authorization=None):
        """Get a direct stream URL for preview playback."""
        try:
            # Get track data to find transcodings
            track_data = self._get(f'tracks/{track_id}')
            
            if not track_data.get('streamable'):
                return None
            
            transcodings = track_data.get('media', {}).get('transcodings', [])
            if not transcodings:
                return None
            
            # Use track_authorization from track_data if not provided
            auth = track_authorization or track_data.get('track_authorization', '')
            
            # Find best stream for preview (prefer progressive over HLS)
            best_url = None
            for transcoding in transcodings:
                protocol = transcoding.get('format', {}).get('protocol', '')
                url = transcoding.get('url', '')
                
                if url:
                    if protocol == 'progressive':
                        # Progressive is preferred, resolve and return immediately
                        try:
                            resolved_url = self.get_track_stream_link(url, auth)
                            return resolved_url
                        except:
                            continue
                    elif protocol == 'hls' and not best_url:
                        # Store HLS as fallback
                        best_url = url
            
            # If only HLS available, try to resolve it
            if best_url:
                try:
                    resolved_url = self.get_track_stream_link(best_url, auth)
                    return resolved_url
                except:
                    pass
            
            return None
        except Exception as e:
            print(f"[SoundCloud] Error getting preview stream: {e}")
            return None


    def resolve_url(self, url):
        return self._get('resolve', {'url': url})


    def search(self, query_type, query, limit = 10):
        return self._get('search/' + query_type, {'limit': limit, 'top_results': 'v2', 'q': query})
    

    def get_user_albums_tracks(self, user_id):
        # Prefer numeric id; resolve permalink to id when user_id is non-numeric
        uid = user_id
        if isinstance(uid, str) and not uid.isdigit():
            try:
                resolved = self._get('resolve', {'url': f'https://soundcloud.com/{uid}'})
                if isinstance(resolved, dict):
                    uid = resolved.get('id') or (resolved.get('urn') or '').split(':')[-1] or uid
                else:
                    uid = getattr(resolved, 'id', uid)
            except Exception:
                pass
        uid = str(uid) if uid is not None else str(user_id)
        # Request without linked_partitioning; handle list or dict response and paginate via next_href
        album_err = None
        track_err = None
        try:
            album_items = self._get_collection_paginated(
                f'users/{uid}/albums',
                {'limit': 200}
            )
            album_data = {i['id']: i for i in album_items}
        except Exception as e:
            album_data = {}
            album_err = e
        try:
            track_items = self._get_collection_paginated(
                f'users/{uid}/tracks',
                {'limit': 200}
            )
            track_data = {i['id']: i for i in track_items}
        except Exception as e:
            track_data = {}
            track_err = e
        if not album_data and not track_data and (album_err or track_err):
            def _is_restricted(e):
                msg = str(e).lower() if e else ""
                return "403" in msg or "restricted" in msg or "not available" in msg
            if _is_restricted(album_err) or _is_restricted(track_err):
                print(f"[SoundCloud] User uid={uid}: content not available (restricted or disabled for API).")
            else:
                err_parts = []
                if album_err:
                    err_parts.append(f"albums: {album_err}")
                if track_err:
                    err_parts.append(f"tracks: {track_err}")
                print(f"[SoundCloud] get_user_albums_tracks(uid={uid}) failed: {'; '.join(err_parts)}")
        return album_data, track_data
    

    def get_tracks_from_tracklist(self, track_data): # WHY?! Only the web player's api-v2 needs this garbage, not api or api-mobile
        tracks_to_get = [str(i['id']) for i in track_data if 'streamable' not in i]
        tracks_to_get_chunked = [tracks_to_get[i:i + 50] for i in range(0, len(tracks_to_get), 50)]
        new_track_data = {j['id']: j for j in sum([self._get('tracks', {'ids': ','.join(i)}) for i in tracks_to_get_chunked], [])}
        return {i['id']: (i if 'streamable' in i else new_track_data[i['id']]) for i in track_data}
