# Lazy import ffmpeg to avoid circular import issues in PyInstaller bundles
ffmpeg = None

def _get_ffmpeg():
    """Lazily import ffmpeg module."""
    global ffmpeg
    if ffmpeg is None:
        import ffmpeg as _ffmpeg
        ffmpeg = _ffmpeg
    return ffmpeg

import re

from utils.models import *
from utils.utils import create_temp_filename, download_to_temp, silentremove
from .soundcloud_api import SoundCloudWebAPI


module_information = ModuleInformation(
    service_name = 'SoundCloud',
    module_supported_modes = ModuleModes.download,
    session_settings = {'web_access_token': ''},
    netlocation_constant = 'soundcloud',
    test_url = 'https://soundcloud.com/alanwalker/darkside-feat-tomine-harket-au',
    url_decoding = ManualEnum.manual,
    login_behaviour = ManualEnum.manual
)


class ModuleInterface:
    def __init__(self, module_controller: ModuleController):
        self.exception = module_controller.module_error
        settings = module_controller.module_settings
        self.websession = SoundCloudWebAPI(settings['web_access_token'], module_controller.module_error)
        self.module_controller = module_controller

        self.artists_split = lambda artists_string: artists_string.replace(' & ', ', ').replace(' and ', ', ').replace(' x ', ', ').split(', ')
        self.artwork_url_format = lambda artwork_url: artwork_url.replace('-large', '-original') if artwork_url else None
    

    @staticmethod
    def get_release_year(data):
        release_date = ''
        if 'release_date' in data and data['release_date']:
            release_date = data['release_date']
        elif 'display_date' in data and data['display_date']:
            release_date = data['display_date']
        elif 'created_at' in data and data['created_at']:
            release_date = data['created_at']
        return int(release_date.split('-')[0])


    def custom_url_parse(self, link):
        types_ = {'user': DownloadTypeEnum.artist, 'track': DownloadTypeEnum.track, 'playlist': DownloadTypeEnum.playlist}
        result = self.websession.resolve_url(link)
        type_ = types_[result['kind']] if result['kind'] != 'playlist' or (result['kind'] == 'playlist' and not result['is_album']) else DownloadTypeEnum.album
        id_ = result['id']
        
        return MediaIdentification(
            media_type = type_,
            media_id = id_,
            extra_kwargs = {'data': {id_: result}}
        )


    def search(self, query_type: DownloadTypeEnum, query, tags: Tags = None, limit = 10):
        if query_type is DownloadTypeEnum.artist:
            qt = 'users'
        elif query_type is DownloadTypeEnum.playlist:
            qt = 'playlists_without_albums'
        elif query_type is DownloadTypeEnum.album:
            qt = 'albums'
        elif query_type is DownloadTypeEnum.track:
            qt = 'tracks'
        else:
            raise self.exception(f'Query type {query_type.name} is unsupported')
        results = self.websession.search(qt, query, limit)
        
        search_results = []
        for result in results['collection']:
            # Get cover/artwork URL
            image_url = None
            if qt == 'users':
                # For artists, use avatar
                image_url = result.get('avatar_url')
            elif qt in ('playlists_without_albums', 'albums'):
                # For playlists/albums, try multiple sources:
                # 1. Playlist's own artwork_url
                # 2. calculated_artwork_url (auto-generated from tracks)
                # 3. First track's artwork (if tracks are included in search results)
                # Note: We DON'T fall back to user avatar for playlists - that shows a generic person icon
                image_url = result.get('artwork_url')
                if not image_url:
                    image_url = result.get('calculated_artwork_url')
                if not image_url:
                    # Try to get artwork from first track
                    tracks = result.get('tracks', [])
                    if tracks and len(tracks) > 0:
                        first_track = tracks[0]
                        image_url = first_track.get('artwork_url')
                # Skip user avatar fallback for playlists - it's just a generic person icon
            else:
                # For tracks, use artwork or fallback to user avatar
                image_url = result.get('artwork_url') or (result.get('user', {}).get('avatar_url') if result.get('user') else None)
            
            # Skip default/placeholder avatar URLs (they show generic person icons)
            if image_url and 'default_avatar' in image_url:
                image_url = None
            
            # Convert to smaller size for thumbnails (use -t200x200 for search results)
            if image_url:
                image_url = image_url.replace('-large', '-t200x200')
            
            # Preview URL for tracks - leave as None, will be lazy-loaded on click
            # SoundCloud requires resolving stream URLs which needs API authentication
            preview_url = None
            # Note: Track is streamable if result.get('streamable') is True
            # The actual stream URL will be fetched on-demand via lazy loading
            
            # Get duration for tracks
            duration = None
            if qt == 'tracks' and result.get('duration'):
                duration = result['duration'] // 1000  # Convert ms to seconds
            elif qt in ('albums', 'playlists_without_albums') and result.get('duration'):
                duration = result['duration'] // 1000
            
            # Get year
            year = None
            if result.get('release_date'):
                year = result['release_date'].split('-')[0]
            elif result.get('display_date'):
                year = result['display_date'].split('-')[0]
            elif result.get('created_at'):
                year = result['created_at'].split('-')[0]
            
            search_results.append(SearchResult(
                result_id = result['id'],
                name = result['title'] if qt != 'users' else result['username'],
                artists = self.artists_split(result['user']['username']) if qt != 'users' else None,
                year = year,
                duration = duration,
                image_url = image_url,
                preview_url = preview_url,
                extra_kwargs = {'data': {result['id'] : result}}
            ))
        
        return search_results


    def get_track_download(self, track_url, download_url, codec, track_authorization, **kwargs):
        explicit_is_hls_from_kwargs = kwargs.get('is_hls')
        determined_is_hls = False

        if explicit_is_hls_from_kwargs is True:
            determined_is_hls = True
        elif isinstance(track_url, str) and \
             ('/hls' in track_url.lower() or \
              '.m3u8' in track_url.lower() or \
              'ctr-encrypted-hls' in track_url.lower()):
            determined_is_hls = True
        
        is_hls = determined_is_hls

        access_token = self.websession.access_token

        if is_hls:
            if not track_url:
                raise self.exception("HLS stream URL not found in get_track_download (is_hls path)")
            
            m3u8_url_resolved = None
            try:
                m3u8_url_resolved = self.websession.get_track_stream_link(track_url, track_authorization)
                if not m3u8_url_resolved or not isinstance(m3u8_url_resolved, str) or not m3u8_url_resolved.startswith('http'):
                    raise self.exception(f"HLS_INVALID_M3U8_URL: Resolved M3U8 URL is invalid: {m3u8_url_resolved}")
            except Exception as e:
                raise self.exception(f"HLS_M3U8_RESOLUTION_ERROR: Failed to resolve M3U8 stream link: {e}")

            extension = codec_data[codec].container.name if codec in codec_data else 'm4a'
            output_location = create_temp_filename() + '.' + extension
            
            ffmpeg_input_options = {
                # 'f': 'hls', # Usually auto-detected, can be omitted
                'hide_banner': None,
                'y': None, 
                'headers': f'Authorization: OAuth {access_token}\r\n',
                'protocol_whitelist': 'http,https,tls,tcp,file,crypto'
            }
            ffmpeg_output_options = {
                'acodec': 'copy',
                'loglevel': 'error' 
            }

            try:
                _ffmpeg = _get_ffmpeg()
                process = _ffmpeg.input(m3u8_url_resolved, **ffmpeg_input_options).output(output_location, **ffmpeg_output_options).run_async(pipe_stdout=True, pipe_stderr=True)
                out, err = process.communicate()

                if process.returncode != 0:
                    silentremove(output_location)
                    stderr_output = err.decode('utf8', errors='ignore') if err else "No stderr from process"
                    raise self.exception(f"HLS_DOWNLOAD_FFMPEG_ERROR: FFmpeg process failed (RC: {process.returncode}). Stderr: {stderr_output}")

            except _get_ffmpeg().Error as e:
                silentremove(output_location)
                stderr_log = e.stderr.decode('utf8', errors='ignore') if hasattr(e, 'stderr') and e.stderr else "No direct stderr from ffmpeg.Error object"
                raise self.exception(f"HLS_FFMPEG_LIB_ERROR: {stderr_log}. Original error: {e}")
            except Exception as e:
                silentremove(output_location)
                raise self.exception(f"HLS_UNEXPECTED_ERROR_IN_TRY_BLOCK: {e}")
            
            return TrackDownloadInfo(
                download_type = DownloadEnum.TEMP_FILE_PATH,
                temp_file_path = output_location
            )
        
        auth_header_non_hls = {"Authorization": f"OAuth {access_token}"}
        if not download_url: 
            resolved_stream_url = self.websession.get_track_stream_link(track_url, track_authorization)
        else:
            resolved_stream_url = download_url

        if codec == CodecEnum.AAC:
            extension = codec_data[codec].container.name
            temp_location = download_to_temp(resolved_stream_url, auth_header_non_hls, extension)
            output_location = create_temp_filename() + '.' + extension
            try:
                _get_ffmpeg().input(temp_location).output(output_location, acodec='copy', loglevel='error').run()
                silentremove(temp_location)
            except Exception as e:
                silentremove(output_location)
                print(f'FFmpeg is not installed or working properly for AAC remux! Error: {e}. Using fallback, may have errors.')
                output_location = temp_location

            return TrackDownloadInfo(
                download_type = DownloadEnum.TEMP_FILE_PATH,
                temp_file_path = output_location
            )
        else:
            return TrackDownloadInfo(
                download_type = DownloadEnum.URL,
                file_url = resolved_stream_url,
                file_url_headers = auth_header_non_hls
            )


    def _parse_aac_bitrate_from_preset(self, preset_string):
        # preset_string is like "aac_256k" or "aac_1_0"
        if not isinstance(preset_string, str):
            return 0
        
        # Handle aac_XXXk format (e.g., aac_256k)
        match_kbps = re.search(r'aac_(\d+)k', preset_string)
        if match_kbps:
            try:
                return int(match_kbps.group(1))
            except ValueError:
                return 0 # Should not happen if regex matches

        # Handle other aac_ formats like aac_1_0, assign a default quality
        if preset_string.startswith('aac_'):            
            return 64 

        return 0 # Default if no clear bitrate is found or format is unexpected

    def _parse_progressive_bitrate_from_preset(self, preset_string, stream_codec_name):
        if not isinstance(preset_string, str):
            return 0

        # Specific SoundCloud Opus presets (these are descriptive, not direct bitrates)
        if stream_codec_name == 'OPUS':
            if 'abr_hq' in preset_string: return 128 # Approximate for high quality Opus
            if 'abr_sq' in preset_string: return 96  # Approximate for standard quality Opus
        
        # Generic pattern for mp3_XXXk or opus_XXXk or aac_XXXk
        match_kbps = re.search(r'(\d+)k', preset_string)
        if match_kbps:
            try: return int(match_kbps.group(1))
            except ValueError: pass

        # Generic pattern for mp3_XXX or opus_XXX or aac_XXX (where XXX is bitrate)
        parts = preset_string.split('_')
        if len(parts) > 1 and parts[1].isdigit():
            try: return int(parts[1])
            except ValueError: pass
        
        # Fallback for Opus quality levels like opus_X_Y (0-10 for X in opusenc)
        # Scale X to an approximate bitrate range (e.g. X*12 might map 0-10 to 0-120kbps range)
        if stream_codec_name == 'OPUS' and len(parts) > 1 and parts[0] == 'opus' and parts[1].isdigit():
            try: 
                quality_level = int(parts[1])
                # Simple scaling: maps 0-10 to something in a typical bitrate range                
                return quality_level * 12 + 32 # e.g. 0->32, 5->92, 8->128, 10->152
            except ValueError: pass

        return 0 # Default

    def get_track_info(self, track_id, quality_tier: QualityEnum, codec_options: CodecOptions, data={}):
        track_data = data[track_id] if track_id in data else self.websession._get('tracks/' + track_id)
        metadata = track_data.get('publisher_metadata') or {}

        file_url, download_url, final_codec, error = None, None, CodecEnum.AAC, None
        final_is_hls_stream = False

        if track_data['downloadable'] and track_data['has_downloads_left']:
            download_url = self.websession.get_track_download(track_id)
            content_type_header = self.websession.s.head(download_url).headers.get('Content-Type', '')
            codec_str_part = content_type_header.split('/')[-1]
            codec_str = codec_str_part.replace('mpeg', 'mp3').replace('ogg', 'vorbis').upper()
            if codec_str in CodecEnum.__members__:
                final_codec = CodecEnum[codec_str]
            else:
                error = f"Unknown codec from direct download Content-Type: {content_type_header}"
                final_codec = CodecEnum.AAC # Default
            final_is_hls_stream = False
            # For direct downloads, file_url is not used; download_url is primary.            
            file_url = download_url 

        elif track_data['streamable']:
            if track_data['media']['transcodings']:
                available_streams = []
                # Codec preference for tie-breaking (higher is better)
                # Prefer HLS slightly if quality is identical
                codec_preference = {
                    (CodecEnum.AAC, True): 5,  # HLS AAC
                    (CodecEnum.OPUS, True): 4, # HLS Opus
                    (CodecEnum.OPUS, False): 3,# Progressive Opus
                    (CodecEnum.AAC, False): 2, # Progressive AAC
                    (CodecEnum.MP3, False): 1, # Progressive MP3
                    (CodecEnum.MP3, True): 0,  # HLS MP3 (less common, lower preference)
                }

                for i in track_data['media']['transcodings']:
                    protocol = i['format']['protocol']
                    preset_string = i['preset']
                    preset_parts = preset_string.split('_')
                    stream_codec_name = preset_parts[0].upper() if preset_parts else ''

                    if stream_codec_name in CodecEnum.__members__:
                        current_codec_enum = CodecEnum[stream_codec_name]
                        
                        # Determine if it's an HLS stream more robustly
                        stream_transcoding_url_for_check = i['url']
                        is_hls_by_protocol = (protocol == 'hls')
                        is_hls_by_url = (isinstance(stream_transcoding_url_for_check, str) and \
                                         ('/hls' in stream_transcoding_url_for_check.lower() or \
                                          '.m3u8' in stream_transcoding_url_for_check.lower() or \
                                          'ctr-encrypted-hls' in stream_transcoding_url_for_check.lower() or \
                                          'cbc-encrypted-hls' in stream_transcoding_url_for_check.lower()))
                        is_hls = is_hls_by_protocol or is_hls_by_url
                        
                        # Determine if it's an ENCRYPTED HLS stream
                        is_encrypted_hls = False
                        if is_hls and isinstance(stream_transcoding_url_for_check, str) and \
                           ('ctr-encrypted-hls' in stream_transcoding_url_for_check.lower() or \
                            'cbc-encrypted-hls' in stream_transcoding_url_for_check.lower()):
                            is_encrypted_hls = True
                        # End of new HLS determination logic
                        
                        quality_score = 0 # Renamed from 'quality'

                        if is_encrypted_hls:
                            quality_score = -100 # Heavily penalize encrypted streams
                        elif is_hls:
                            if current_codec_enum == CodecEnum.AAC:
                                quality_score = self._parse_aac_bitrate_from_preset(preset_string)
                            # Add parsing for HLS Opus/MP3 bitrates if their presets have them
                            # For now, relying on generic progressive parser or default 0 for other HLS
                            else: 
                                quality_score = self._parse_progressive_bitrate_from_preset(preset_string, stream_codec_name)
                        else: # Progressive
                            quality_score = self._parse_progressive_bitrate_from_preset(preset_string, stream_codec_name)
                        
                        if i['url'] and quality_score >= 0: # Only consider streams with a URL and non-negative quality
                            pref_score = codec_preference.get((current_codec_enum, is_hls), 0)
                            available_streams.append({
                                'url': i['url'],
                                'codec': current_codec_enum,
                                'is_hls': is_hls,
                                'is_encrypted': is_encrypted_hls,
                                'quality': quality_score,
                                'preference': pref_score,
                                'preset': preset_string
                            })
                
                if available_streams:
                    # Sort: 1. quality (desc), 2. preference score (desc)
                    available_streams.sort(key=lambda x: (x['quality'], x['preference']), reverse=True)
                    
                    best_stream = available_streams[0]
                    # Ensure the best stream has a positive quality, otherwise it might be an undesired low-quality default
                    # Always set these from the best stream found
                    file_url = best_stream['url']
                    final_codec = best_stream['codec']
                    final_is_hls_stream = best_stream['is_hls']
                    final_is_encrypted_hls = best_stream.get('is_encrypted', False)
                    
                    if final_is_encrypted_hls:
                        error = "Track is available as a DRM-protected HLS stream, which can be downloaded, but aren't playable without decryption key. Skipping."
                    elif best_stream['quality'] > 0:
                        error = None # Clear previous errors if a good stream is found
                    else:
                        # This case means the best found stream had quality 0 or less (e.g. only failed parsing or was encrypted)
                        if not final_is_encrypted_hls: # Don't overwrite specific DRM error
                            error = f"Best stream found (preset: {best_stream['preset']}) has zero or negative quality, or codec could not be parsed."
                else:
                    error = "No stream transcodings found or none were usable."
            else:
                error = "No stream transcodings available for this track."
        else:
            error = "Track is not available for download or streaming."
        
        return TrackInfo(
            name = track_data['title'].split(' - ')[1] if ' - ' in track_data['title'] else track_data['title'],
            album = metadata.get('album_title'),
            album_id = '',
            artists = self.artists_split(metadata['artist'] if metadata.get('artist') else track_data['user']['username']),
            artist_id = '' if 'artist' in metadata else track_data['user']['permalink'],
            download_extra_kwargs = {
                'track_url': file_url, 
                'download_url': download_url, 
                'codec': final_codec, 
                'track_authorization': track_data['track_authorization'],
                'is_hls': final_is_hls_stream
            },
            codec = final_codec,
            sample_rate = 48,
            release_year = self.get_release_year(track_data),
            cover_url = self.artwork_url_format(track_data.get('artwork_url') or track_data['user']['avatar_url']),
            explicit = metadata.get('explicit'),
            error = error,
            tags =  Tags(
                track_number = int(list(data.keys()).index(track_id)) + 1 if data.get(track_id) else 1,
                release_date = track_data['created_at'].split('T')[0] if track_data.get("created_at") else None,
                genres = track_data['genre'].split('/') if track_data.get('genre') else None,
                composer = metadata.get('writer_composer'),
                copyright = metadata.get('p_line'),
                upc = metadata.get('upc_or_ean'),
                isrc = metadata.get('isrc')
            )
        )
    

    def get_album_info(self, album_id, data: dict) -> AlbumInfo | None:
        if not album_id:  # This will be true if album_id is None or an empty string
            if self.module_controller.orpheus_options.debug_mode:
                self.module_controller.printer_controller.oprint(f"[SoundCloud] get_album_info: Called with an empty or None album_id. Cannot fetch album details.")
            return None
        
        # Attempt to get data from the provided dict first
        playlist_data = data[album_id]
        playlist_tracks = self.websession.get_tracks_from_tracklist(playlist_data['tracks']) if playlist_data.get('tracks') else {}
        return AlbumInfo(
            name = playlist_data['title'],
            artist = playlist_data['user']['username'],
            artist_id = playlist_data['user']['permalink'],
            cover_url = self.artwork_url_format(playlist_data.get('artwork_url') or playlist_data['user']['avatar_url']),
            release_year = self.get_release_year(playlist_data),
            tracks = list(playlist_tracks.keys()),
            track_extra_kwargs = {'data': playlist_tracks}
        )
    

    def get_playlist_info(self, playlist_id, data):
        playlist_data = data[playlist_id]
        playlist_tracks = self.websession.get_tracks_from_tracklist(playlist_data['tracks'])
        return PlaylistInfo(
            name = playlist_data['title'],
            creator = playlist_data['user']['username'],
            creator_id = playlist_data['user']['permalink'],
            cover_url = self.artwork_url_format(playlist_data['artwork_url']),
            release_year = self.get_release_year(playlist_data),
            tracks = list(playlist_tracks.keys()),
            track_extra_kwargs = {'data': playlist_tracks}
        )


    def get_artist_info(self, artist_id, get_credited_albums, data):
        album_data, track_data = self.websession.get_user_albums_tracks(artist_id)
        return ArtistInfo(
            name = data[artist_id]['username'],
            albums = list(album_data.keys()),
            album_extra_kwargs = {'data': album_data},
            tracks = list(track_data.keys()),
            track_extra_kwargs = {'data': track_data}
        )