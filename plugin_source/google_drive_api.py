import os
import io
import json
import zipfile
import sys

import aqt
from aqt import mw
from aqt.qt import *
import requests

sys.path.append(os.path.join(os.path.dirname(__file__), "dist"))

from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError
from googleapiclient.discovery import build
from google.oauth2 import service_account


class GoogleDriveAPI:
    def __init__(self, service_account, folder_id):
        self.SCOPES = ['https://www.googleapis.com/auth/drive.file']
        self.SERVICE_ACCOUNT = service_account
        self.FOLDER_ID = folder_id
        self.creds = None
        self.service = None
        self._set_up_credentials()
        self._set_up_service()
    
    def _set_up_credentials(self):
        self.creds = service_account.Credentials.from_service_account_info(
            self.SERVICE_ACCOUNT,
            scopes=self.SCOPES
        )
        
    def _handle_http_error(self, error):
        if isinstance(error, HttpError):
            error_message = error._get_reason()
        else:
            error_message = str(error)
        print(f"[GDrive] An error occurred: {error_message}")

    def _set_up_service(self):
        self.service = build('drive', 'v3', credentials=self.creds, cache_discovery=False)
          
    def chunks(self, lst, chunk_size):
        """Yield successive chunk_size-sized chunks from lst."""
        for i in range(0, len(lst), chunk_size):
            yield lst[i:i + chunk_size]
             
    def download_selected_files_as_zip(self, file_names, local_folder_path, download_progress_cb=None):
        counter = 0
        maxx = len(file_names)
        for chunk in self.chunks(file_names, 50):  # break up file_names into chunks of 100
            if mw.progress.want_cancel():
                break
            query = f"("
            query += " or ".join([f"name='{file_name}'" for file_name in chunk])
            query += ")"
            res = self._download_files(self.query_files(query), local_folder_path, maxx, counter, download_progress_cb)
            if res < 0: # Abort because something is wrong
                continue
            counter += res

            
    def query_files(self, query):
        files = []
        try:            
            page_token = None
            while True:
                response = self.service.files().list(q=query,
                                                supportsAllDrives=True,
                                                includeItemsFromAllDrives=True,
                                                fields='nextPageToken, '
                                                    'files(id, name)',
                                                pageToken=page_token).execute()
                files.extend(response.get('files', []))
                page_token = response.get('nextPageToken', None)
                if page_token is None:
                    break
    
        except HttpError as error:
            self._handle_http_error(error)
        
        return files

    def list_media_files_in_folder(self):
        query = f"mimeType != 'application/vnd.google-apps.folder' and trashed=false"
        return self.query_files(query)
    
    def _download_files(self, items, local_folder_path, total_files, curr_amount, download_progress_cb) -> int:
        counter = 0
        try:
            if items is None or len(items) == 0:
                print('No media files found.')
                return -1
    
            added_file_names = set()
            zip_path = os.path.join(local_folder_path, 'media_files.zip')
            with zipfile.ZipFile(zip_path, 'w') as zip_file:
                for item in items:
                    # Download each file and add it to the zip file if its not a duplicate
                    file_name = item['name']
                    if file_name not in added_file_names:
                        added_file_names.add(file_name)
                        request = self.service.files().get_media(fileId=item['id'])
                        file_bytes = io.BytesIO(request.execute())
                        zip_file.writestr(file_name, file_bytes.getvalue())
                        counter += 1

                    # Update the download progress
                    if download_progress_cb:
                        download_progress_cb(int(curr_amount + counter), int(total_files))
                        
                    if mw.progress.want_cancel():
                        break

            with zipfile.ZipFile(zip_path, 'r') as zip_file:
                zip_file.extractall(local_folder_path)

            os.remove(zip_path)
            
            return counter

        except HttpError as error:
            self._handle_http_error(error)
            return -2

    def upload_files_to_folder(self, base_path, file_names, upload_progress_cb=None):
        try:            
            existing_media = self.list_media_files_in_folder()
            missing_media = [media for media in file_names if not any(media_name['name'] == media for media_name in existing_media) and os.path.exists(os.path.join(base_path, media))]
                
            file_ids = []
            total_files = len(missing_media)
            
            for file_name in missing_media:
                file_path = os.path.join(base_path, file_name) 
                file_metadata = {
                    'name': file_name,
                    'parents': [self.FOLDER_ID],
                }
                media = MediaFileUpload(file_path, resumable=True)
                file = self.service.files().create(body=file_metadata, media_body=media, fields='id').execute()
                file_ids.append(file.get('id'))
            
                if upload_progress_cb:
                    upload_progress_cb(int(len(file_ids)), int(total_files))
                    
                if mw.progress.want_cancel():
                    break

            return file_ids

        except HttpError as error:
            self._handle_http_error(error)

def update_gdrive_data(deck_hash, gdrive_new):
    strings_data = mw.addonManager.getConfig(__name__)
    if strings_data:
        for sub, details in strings_data.items():
            if sub == deck_hash:
                details["gdrive"] = gdrive_new
                break
        mw.addonManager.writeConfig(__name__, strings_data)
        
def get_gdrive_data(deck_hash):
    strings_data = mw.addonManager.getConfig(__name__)
    if strings_data:        
        for sub, details in strings_data.items():
            if sub == deck_hash:                
                if "gdrive" not in details or len(details["gdrive"]) == 0 or details["gdrive"]["folder_id"] == "":
                    break
                return details["gdrive"]
    # GDrive data not found, see if we can find it on the server
    response = requests.get("https://plugin.ankicollab.com/GetGDriveData/" + deck_hash)
    if response and response.status_code == 200:
        res = response.text
        if res is not None and res:
            gdrive_data = json.loads(res)
            update_gdrive_data(deck_hash, gdrive_data)
            return gdrive_data
        print("GDrive data not found on server")
    return None