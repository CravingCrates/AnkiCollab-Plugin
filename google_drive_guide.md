# How to Set Up Google Drive with AnkiCollab

AnkiCollab is a collaborative tool for sharing Anki decks. It allows you to share your decks with your subscribers and create a collaborative community of users who can contribute and learn together. However, one of the limitations of AnkiCollab is that it doesn't support media files directly, and this can be a problem if your decks rely heavily on images or audio. Fortunately, you can use Google Drive to store and share your media files with your subscribers. In this guide, we'll walk you through the steps to set up Google Drive with AnkiCollab.

## Step 1: Create a Google Account

If you don't have a Google account, you'll need to create one. You can use your existing Google account, but we recommend creating a new one so that your subscribers will have limited access to it.

1. Go to the [Google Cloud Console](https://console.cloud.google.com/).
2. Click on "Select a project".
   ![Select a Project](https://i.imgur.com/RvxABbx.png)
3. Create a new project.
   ![Create a New Project](https://i.imgur.com/SQgWopA.png)
4. Enter a name for the project and click "Create".
   ![Enter Project Name](https://i.imgur.com/VwCB82r.png)
5. Select the project.
   ![Select Project](https://i.imgur.com/4lZ8MEl.png)

## Step 2: Enable Google Drive API

1. Open the [Google Drive API](https://console.cloud.google.com/marketplace/product/google/drive.googleapis.com) page in your Google Cloud Console.
2. Enable the API by clicking the "Enable" button.
   ![Enable API](https://i.imgur.com/PatVe37.png)
3. A new page will open. Click on "Credentials".
   ![Click on Credentials](https://i.imgur.com/E94wiUk.png)
4. Click on "Create Credentials".
   ![Create Credentials](https://i.imgur.com/l6k28rb.png)
5. Select "Service account" and enter a name for the account.
   ![Service Account](https://i.imgur.com/OCjwYCe.png)
6. Click "Done" to create the account.
   ![Create Account](https://i.imgur.com/TokyLe4.png)
7. Store the email address displayed on the screen somewhere safe.
   ![Store Email Address](https://i.imgur.com/7QHcuaL.png)
8. Click on "Keys".
   ![Click on Keys](https://i.imgur.com/r4fC6sD.png)
9. Create a new key by clicking on the "Add Key" button and selecting "JSON".
   ![Create New Key](https://i.imgur.com/0fAxJ7S.png)
10. Click "Create" to download the JSON file.
    ![Download JSON File](https://i.imgur.com/ZnYi9nZ.png)

Congratulations! You have now completed the hard part of setting up Google Drive with AnkiCollab.

## Step 3: Create a Folder in Google Drive

Go to [Google Drive](https://drive.google.com/) and create a new folder by right-clicking somewhere and selecting "New Folder"

![new folder](https://i.imgur.com/vz7NcgR.png)

Create a new folder and open it.

![open folder](https://i.imgur.com/kClktlZ.png)

Note that every folder in Google Drive has a unique name worldwide, and we need that unique name to identify the folder that AnkiCollab should put the media in. To obtain this unique identifier, copy the part of the URL after "folders/". In my case, it is `17RJYNO1JlX8veXUedPVlOgTt9jqeV0v-`.

Step 11:
As a final step, invite the service account we created earlier to your folder so that it has access. Click "Manage Access" in the folder's context menu.

![manage access](https://i.imgur.com/dSiPNaB.png)

Enter the email address you stored earlier. If you forgot to save it, you can find it again on the "Credentials" page in the Google Cloud Console.

![enter email address](https://i.imgur.com/ly6FaPo.png)

Disable notifications and send the invite.

![send invite](https://i.imgur.com/3KlT786.png)

The folder should now have two people with access: you and your service account.

![folder access](https://i.imgur.com/rAzqXVe.png)

Bonus Step:
If you want to enable everyone on the Ankicollab website to view the media files, you can allow "General access" to "Anyone with the link" by clicking "Manage access" again.

![general access](https://i.imgur.com/BTM5uJM.png)

This will enable everyone on the website to view the media files (but not edit them, of course).

Congratulations! You are now 99% done. The final step is to set up your Google media in Ankicollab.

Step 12:
Navigate to https://www.ankicollab.com/ManageDecks/ and select your deck.

![select deck](https://i.imgur.com/TnAcIWI.png)

Click "Set up" next to "Google Media".

![set up google media](https://i.imgur.com/0hykjyP.png)

Drag and drop or select the file that was downloaded earlier.

![upload file](https://i.imgur.com/DdUpkSx.png)

The upload should be confirmed with blue text.

![upload confirmation](https://i.imgur.com/0VX59vb.png)

Enter the folder ID that you extracted earlier.

![enter folder ID](https://i.imgur.com/e5VZu76.png)

Finally, hit "Save" and you're done!

![save](https://i.imgur.com/U19pSVV.png)

Note that you and your subscribers need to sync on Anki with Ankicollab, and the next time somebody creates a new card with a media file, it will automatically be uploaded to the Google Drive folder.

If you don't start from scratch with your deck, you can export the existing media from your deck and upload it to the Google Drive folder manually. This way, all media files will be downloaded automatically the next time a subscriber syncs.

Your existing subscribers who had to live without media so far can quickly download all media files with one button.

![download media](https://i.imgur.com/mQ2fc7t.png)

Congratulations again, and enjoy using Ankicollab with your Google Drive storage solution!