import os
import io
import json
import zipfile
import sys

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
        data = error.read()
        error = json.loads(data.decode('utf-8'))
        print(error['error']['message'])
        
    def _set_up_service(self):
        self.service = build('drive', 'v3', credentials=self.creds)
           
    def download_selected_files_as_zip(self, file_names, local_folder_path, download_progress_cb=None):
        query = f"("
        query += " or ".join([f"name='{file_name}'" for file_name in file_names])
        query += ")"
        return self._download_files_by_query(query, local_folder_path, download_progress_cb)
    
    def query_files(self, query):
        files = []
        try:            
            page_token = None
            while True:
                response = self.service.files().list(q=query,
                                                spaces='drive',
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
        query = f"mimeType contains 'image/' or mimeType contains 'video/' or mimeType contains 'audio/'"
        return self.query_files(query)
    
    def _download_files_by_query(self, query, local_folder_path, download_progress_cb) -> int:
        try:                
            items = self.query_files(query)

            if items is None:
                print('No media files found.')
                return -1
    
            added_file_names = set()
            zip_path = os.path.join(local_folder_path, 'media_files.zip')
            total_files = len(items)
            downloaded_files = 0

            with zipfile.ZipFile(zip_path, 'w') as zip_file:
                for item in items:
                    # Download each file and add it to the zip file if its not a duplicate
                    file_name = item['name']
                    if file_name not in added_file_names:
                        added_file_names.add(file_name)
                        request = self.service.files().get_media(fileId=item['id'])
                        file_bytes = io.BytesIO(request.execute())
                        zip_file.writestr(file_name, file_bytes.getvalue())

                    # Update the download progress
                    downloaded_files += 1
                    progress = downloaded_files / total_files * 100
                    if download_progress_cb:
                        download_progress_cb(int(progress))

            with zipfile.ZipFile(zip_path, 'r') as zip_file:
                zip_file.extractall(local_folder_path)

            os.remove(zip_path)

            return downloaded_files

        except HttpError as error:
            self._handle_http_error(error)
            return -2

    def upload_files_to_folder(self, base_path, file_names, upload_progress_cb=None):
        try:           
            file_ids = []
            total_files = len(file_names)
            
            for file_name in file_names:
                file_path = os.path.join(base_path, file_name) 
                file_metadata = {
                    'name': file_name,
                    'parents': [self.FOLDER_ID],
                }
                media = MediaFileUpload(file_path, resumable=True)
                file = self.service.files().create(body=file_metadata, media_body=media, fields='id').execute()
                file_ids.append(file.get('id'))
            
                progress = len(file_ids) / total_files * 100
                if upload_progress_cb:
                    upload_progress_cb(int(progress))

            return file_ids

        except HttpError as error:
            self._handle_http_error(error)
