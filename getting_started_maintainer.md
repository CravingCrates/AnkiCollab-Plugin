# Getting Started - Maintainer

- [Getting Started - Maintainer](#getting-started---maintainer)
  - [Publishing a Deck](#publishing-a-deck)
    - [Signing Up for AnkiCollab](#signing-up-for-ankicollab)
    - [Adding a Deck to AnkiCollab](#adding-a-deck-to-ankicollab)
    - [Add Media Support](#media-support)
    - [Auto Approve Changes](#auto-approve-changes)
    - [Add Notes and Subdecks](#add-notes-and-subdecks)
  - [Handling Suggestions](#handling-suggestions)
    - [Content Changes](#content-changes)
  - [Credits](#credits)

Maintainers are the ones keeping AnkiCollab decks healthy, accurate, and updated. When subscribers suggest changes, maintainers approve or deny them.

## Publishing a Deck

By publishing a deck, you will be the sole maintainer and it will be associated with your username. You can add more maintainers by their username via the `Manage Decks` Tab on the Website. For more details on that, see the [FAQ](/faq.md#L156) entry.

### Signing Up for AnkiCollab

Create an account by going to [AnkiCollab.com/Login](https://www.ankicollab.com/login) and clicking **Sign Up Here**.

<img src="https://i.imgur.com/VPsLsk5.png" alt="The sign up page for AnkiCollab website" width="600">

### Adding a Deck to AnkiCollab

Inside of Anki, go to the toolbar, and click `AnkiCollab → Publish New Deck`.

![AnkiCollab Dropdown bar](https://i.imgur.com/CsMaZnq.png)

Select the deck you want to upload and enter your username **exactly** as it is on AnkiCollab.com.

![A pop up window from AnkiCollab waiting for the user to choose which deck to publish.](https://i.imgur.com/3z4jR69.png)

After clicking **Publish Deck**, a confirmation message will pop up and you will be able to view your deck on [AnkiCollab.com/Decks](https://www.ankicollab.com/decks).

![A blue arrow pointing to the title of a deck on the AnkiCollab website.](https://i.imgur.com/dSahhBI.png)

Click on the deck and you can see all the cards as well as the **subscription key**. 🔑

![A blue arrow pointing to the subscription key of a deck on the AnkiCollab website.](https://i.imgur.com/lMgbAyS.png)

Congratulations! The deck is ready to be shared with users. All they need is the subscription key and an account of their own.

Users are prompted to log in after they download the Addon, and are linked to the sign-up page if they haven't created an account yet.

Please note that your newly published Deck is set to `Unlisted` and `New Notetype uploads` are disallowed. You can uncheck the first option to make the deck visible under [Explore Decks](https://www.ankicollab.com/decks)

![Public checkbox and NoteType Checkbox](https://i.imgur.com/T0s0N2A.png)


## Media Support

Since early 2025, AnkiCollab hosts its own Media Server supported by [Ankizin](https://www.ankizin.de/).

You can share media files that are in the cards you publish without setting up anything, media files are checked and uploaded automatically when you submit a suggestion.
AnkiCollab checks the media files attached to the cards you suggest and uploads those that are missing on the server, subscribers automatically download new files when they check for new content.

Restrictions for file uploads are as follows:

- The file is too big (optimized size is > 2mb)
- The fileformat is not supported (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp", ".tif", ".tiff" are the supported formats)

Your files will be converted into .webp format to minimize storage usage, invalid file names will automatically be renamed.

Since the media download takes longer than the rest of the update process, it will continue in the background while users can use anki normally, a small bar on the top right will show the download progress.
Please note that you are liable for the content you distribute, so make sure to only use unlicensed material or get permission.

To bulk upload media files, you can also click the gear icon next to your deck and select AnkiCollab → Upload Missing Media

<img src="https://i.imgur.com/kijdL1t.png" alt="bulk upload media option with blue arrow, dark mode" width="400"/><br>

Your subscribers can pull missing media files by using "Download Missing Media" respectively

## Handling Suggestions

As a maintainer, you are responsible for approving or denying changes. There are multiple types, and we'll cover them all here.

## Auto Approve Changes

For convenience purposes, you can let the system auto approve all changes you (or other maintainers) make.

Once you're logged in, check the box. That's all you need to do!

Card deletion requests are always required to be reviewed on the Website to prevent accidental removal of content.

![Auto Approve Checkbox](https://i.imgur.com/IkRpkgi.png)

### Add notes and subdecks
- If you happen to want to add more notes, just select them in your browser (Ctrl + B), hit right click and select "Ankicollab : Bulk suggest notes". You can then review them on the site (if you haven't turned on [auto approve changes](#auto-approve-changes)).

![](https://i.imgur.com/tV2tOga.png)

- If you want to add some subdecks to the deck (keep in mind that once added they can't be removed for now), you can go on the left of the *Browse* window and right click on the master deck which you chose when first publishing, right click and select "suggest on Ankicollab"

![](https://i.imgur.com/zLCt3xV.png)

- If you want to block new note types and/or subdecks to be added to your deck, you can do so by going to the __Manage Decks__ Tab on the AnkiCollab Website, select your deck and choose "*Disable new subdeck creation*" and/or "*Disallow new notetype uploads*". The latter is enabled by default, v.sup.

![Deck Management Options](https://i.imgur.com/RMKAQva.png)

### Content Changes

All the different possible changes are handled the same way. Someone (or you) may submit a change — fixing typos, updating content, formatting, changing tags, or something else. You'll go to the **All Reviews** page on AnkiCollab and approve or deny them.

Let's use a sample card from a trivia deck.

![A flashcard with the front saying "World's Tallest Building" and the back saying "Lotte World Tower."](https://i.imgur.com/JxQGgx5.png)

While the Lotte World Tower is definitely tall, it's not the tallest — that would be the Burj Khalifa. A good contributor corrects the card and suggests the change.

Navigate to [AnkiCollab.com](https://ankicollab.com/) and click **All Reviews** to see suggestions for your published decks.

![A blue arrow pointing to All Reviews on AnkiCollab.com](https://i.imgur.com/wTTUTpV.png)

From here, we see we have a suggestion involving a content error.

![The All Reviews page on AnkiCollab.com](https://i.imgur.com/hWeodp4.png)

When you click on the card, you'll see what the field had before and after the change. If you'd like to see the rest of the fields for context, click **Go To Full Review**.

![A blue arrow pointing to the Go To Full Review button.](https://i.imgur.com/hz45hrb.png)

Here, the rest of the fields are shown.

![The full review page on AnkiCollab.com](https://i.imgur.com/Ij62zmY.png)

From here, or the previous page, simply click the check mark for suggestions you want to keep or the red X to reject them. After that, you're good to go!

### Card deletion

Cards in your deck can be removed by using the the **Request Note Removal** option just below the *Bulk suggest Notes* feature in the browser.

![RemoveNoteRequest in Browser - White Arrow](https://i.imgur.com/lftYyBT.png)

Removal requests disregard the Auto Approve Changes feature, they always require approval on the Website.
## Credits

This guide was written by Andre, and we would like to extend our sincere thanks for his contributions. With his expertise and commitment, this tutorial has become a valuable resource for all those who read it. Thank you, Andre, for your hard work and dedication.
