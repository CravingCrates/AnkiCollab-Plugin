# Getting Started - Maintainer

- [Getting Started - Maintainer](#getting-started---maintainer)
  - [Publishing a Deck](#publishing-a-deck)
    - [Signing Up for AnkiCollab](#signing-up-for-ankicollab)
    - [Adding a Deck to AnkiCollab](#adding-a-deck-to-ankicollab)
    - [Add Media Support](#media-support)
    - [Add Notes and Subdecks](#add-notes-and-subdecks)
  - [Handling Suggestions](#handling-suggestions)
    - [Content Changes](#content-changes)
    - [Tag Changes](#tag-changes)
    - [Card Deletion](#card-deletion)
  - [Auto Approve Changes](#auto-approve-changes)
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

Select the deck you want to upload and enter your username **exactly** as it is on [AnkiCollab.com](AnkiCollab.com).

<img src="https://i.imgur.com/3z4jR69.png" alt="A pop up window from AnkiCollab waiting for the user to choose which deck to publish" width="500">

After clicking **Publish Deck**, a confirmation message will pop up and you will be able to view your deck on [AnkiCollab.com/Decks](https://www.ankicollab.com/decks).

![A blue arrow pointing to the title of a deck on the AnkiCollab website.](https://i.imgur.com/dSahhBI.png)

Click on the deck and you can see all the cards as well as the **subscription key**. 🔑

![A blue arrow pointing to the subscription key of a deck on the AnkiCollab website.](https://i.imgur.com/lMgbAyS.png)

Congratulations! The deck is ready to be shared with users. All they need is the subscription key and an account of their own.

Users are prompted to log in after they download the Addon, and are linked to the sign-up page if they haven't created an account yet.

Please note that your newly published Deck is set to `Unlisted` and `New Notetype uploads` are disallowed. You can uncheck the first option to make the deck visible under [Explore Decks](https://www.ankicollab.com/decks)

![Public checkbox and NoteType Checkbox](https://i.imgur.com/T0s0N2A.png)


### Media Support
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

### Add notes and subdecks
If you happen to want to add more notes, just select them in your browser (Ctrl + B), hit right click and select "Ankicollab : Bulk suggest notes". You can then review them on the site (if you haven't turned on [auto approve changes](#auto-approve-changes)).

![](https://i.imgur.com/tV2tOga.png)

If you want to add some subdecks to the deck (keep in mind that once added they can't be removed for now), you can go on the left of the *Browse* window and right click on the master deck which you chose when first publishing, right click and select "Suggest on AnkiCollab"

This will suggest the whole deck, meaning the Addon will check every note for changes, find new subdecks or new notes and suggest everything at once. If your deck is large, you may want to suggest the new subdeck only - this is also possible and will be quicker.

<img src="https://i.imgur.com/hxs54t4.png" alt="Suggest on Ankicollab - Right Click" width="250">

If you want to block new note types and/or subdecks to be added to your deck, you can do so by going to the __Manage Decks__ Tab on the AnkiCollab Website, select your deck and choose `Disable new subdeck creation` and/or `Disallow new notetype uploads`. The latter is enabled by default.

![Deck Management Options](https://i.imgur.com/RMKAQva.png)

## Handling Suggestions

As a maintainer, you are responsible for approving or denying changes. There are multiple types, and we'll cover them all here.

### Content Changes
All the different possible changes are handled the same way. Someone (or you) may submit a change — fixing typos, updating content, formatting, changing tags, or something else. You'll go to the `Review changes` page on AnkiCollab and approve or deny them.

Let's use a sample card from the ultimate geography deck:

This note has a mistake — the capital of Sweden is Stockholm, not Oslo. A good contributor corrects the card and suggests the change.

![A flashcard from the UG deck for sweden with "Oslo" in capital field"](https://i.imgur.com/MmlS4Zc.png)

After correcting the content, users need to press the `AnkiCollab` Button:

![The corrected Flashcard with Stockholm - Blue Arrow to AnkiCollab Button](https://i.imgur.com/WJKzooV.png)

The `Commit Information Popup` will appear and the user is prompted to give information on the changes made.

![The corrected Flashcard with Stockholm - PopUp open](https://i.imgur.com/qcSpLHa.png)

After pressing `Submit`, the **Review changes** tab on [AnkiCollab.com](https://ankicollab.com/) will show the open suggestion. This tab contains all open suggestions for all your published decks and those you have maintainer access to:

![A blue arrow pointing to Review Changes on AnkiCollab.com](https://i.imgur.com/IBcJ7bK.png)

Here, we see the open suggestion that was made involving a content error.

When you click on the card, you'll see what content the field had before the change and what changes are suggested, supported by color coding on the right-hand side. If you'd like to see the rest of the fields for context, click `Review Suggestions`.

![A blue arrow pointing to the Go To Full Review button.](https://i.imgur.com/nPcTCT8.png)

Here, the rest of the fields are shown:

<img src="https://i.imgur.com/OFlKxBv.png" alt="Full review page" width="600">

From here, or the previous page, simply click the green `Accept` check mark for suggestions you want to keep or the red `Deny` X to reject them.

After that, you're good to go!

### Tag changes
Tag changes are handled the same way, any user can make changes to tags and use the suggest feature. Maintainers can accept all changes at once by clicking "Accept all", or selectively accepting / denying tag changes one by one.

### Card deletion

Cards in your deck can be removed by using the the **Request Note Removal** option just below the *Bulk suggest Notes* feature in the browser.

<img src="https://i.imgur.com/lftYyBT.png" alt="RemoveNoteRequest in Browser - White Arrow" width="700">

Removal requests disregard the Auto Approve Changes feature, they always require approval on the Website to prevent accidental removal of content.

## Auto Approve Changes

For convenience, you can let the system auto approve all changes you (or other maintainers) make.

Once you're logged in, open the Subscription Manager Pop-Up by Clicking AnkiCollab → "Edit subscriptions"

Open The Global Settings window:

![Edit Subscriptions - Arrow on Global Settings](https://i.imgur.com/Q8tYvQP.png)

Check the Auto Approve Changes (Maintainer only) option and press Save Settings.

![Auto Approve Checkbox - Blue Arrow](https://i.imgur.com/oshaYaB.png)

As mentioned above, Card deletion requests are always required to be reviewed on the Website.

## Credits

This guide was originally written by Andre, whose work and dedication turned it into a resource many can rely on.

A special thanks also goes to Mo, for his key role in AnkiCollab. His bug reports, feature ideas, and updates to this guide have been essential to its growth and improvement, and his steady involvement continues to shape the project’s direction.
