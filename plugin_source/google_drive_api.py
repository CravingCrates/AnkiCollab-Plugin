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
        if isinstance(error, HttpError):
            error_message = error._get_reason()
        else:
            error_message = str(error)
        print(f"An error occurred: {error_message}")

    def _set_up_service(self):
        self.service = build('drive', 'v3', credentials=self.creds)
          
    def chunks(self, lst, chunk_size):
        """Yield successive chunk_size-sized chunks from lst."""
        for i in range(0, len(lst), chunk_size):
            yield lst[i:i + chunk_size]
             
    def download_selected_files_as_zip(self, file_names, local_folder_path, download_progress_cb=None):
        files = []
        for chunk in self.chunks(file_names, 100):  # break up file_names into chunks of 100
            query = f"("
            query += " or ".join([f"name='{file_name}'" for file_name in chunk])
            query += ")"
            files.extend(self.query_files(query))
        return self._download_files(files, local_folder_path, download_progress_cb)

            
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
    
    def _download_files(self, items, local_folder_path, download_progress_cb) -> int:
        try:
            if items is None or len(items) == 0:
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
            
                progress = len(file_ids) / total_files * 100
                if upload_progress_cb:
                    upload_progress_cb(int(progress))

            return file_ids

        except HttpError as error:
            self._handle_http_error(error)
