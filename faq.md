# Frequently Asked Questions

- [Frequently Asked Questions](#frequently-asked-questions)
  - [General](#general)
  - [Subscriber](#subscriber)
  - [Maintainer](#maintainer)

## General

<details close>
<summary><b>Does AnkiCollab handle multiple profiles?</b></summary>
AnkiCollab does not currently respect separate Anki profiles. If you subscribe to a deck on one profile, then switch to another, it will redownload the fresh deck. A workaround for this is to disable the addon. <code>Tools → Add-ons → Select AnkiCollab → Toggle Enabled</code>
</details>

<details close>
<summary><b>Does media get stored on AnkiCollab? How do subscribers get deck media?</b></summary>
AnkiCollab does not upload media (images, audio, etc.) when publishing a deck or suggesting changes. If a deck has media, the maintainer should have a link available for you to download.
</details>

<details close>
<summary><b>How are deck settings handled?</b></summary>
Deck settings — learning steps, new card limits, maximum interval, etc. — are not uploaded when publishing a deck. When a subscriber downloads a deck, their default deck options are assigned. If you want a subscriber to use specific settings, make a note of them in the deck description.
</details>

<details close>
<summary><b>Can I change the name of the deck in my collection?</b></summary>
  Yes, you can rename the uploaded deck and move it around as much as you want. 
  Please note that this does not apply to subdecks of your uploaded deck. Subdecks are required to follow the exact layout from the server. This is necessary for the system to understand where changes are supposed to go and make subdecks possible in the first place. Sorry for the inconvenience.
</details>

## Subscriber

<details close>
<summary><b>Can I exclude certain (personal) tags from uploading, like "leech"?</b></summary>
Yes!<br />
  Open Anki<br />
  Open the Add-ons window (where you install new add-ons), then double-click/Open the Config of AnkiCollab.<br />
  In the respective deck you're trying to edit, locate this line:<br />
  <img src="https://i.imgur.com/HrZyNZu.png" width="50%"><br />
  and add your desired tags to it.<br />
  Separate the tags with commas and enclose them in quotes, like this:<br />
  <img src="https://i.imgur.com/zMyYDxC.png" width="50%">  <br />
  Confirm by clicking OK, and you're done
</details>
<details close>
<summary><b>Can I protect certain cards or fields from being updated?</b></summary>
Yes, you can safeguard specific fields within individual cards to prevent them from being updated. This is particularly useful if you want certain information to remain constant, despite updates. Here's how you can do it:

- **To protect a specific field:** Use the `AnkiCollab_Protect::` command followed by the field name. For example, if you have a card with fields named `Front` and `Back`, and you wish to keep the `Back` field unchanged, add the following tag to your card: `AnkiCollab_Protect::Back`.

- **Handling fields with spaces:** If the field name includes spaces (e.g., `Question Mask`), replace the spaces with underscores when adding the tag, as tags cannot contain spaces. For instance, use `AnkiCollab_Protect::Question_Mask`.

  ![Example Image](https://i.imgur.com/Alpi0VJ.png)

- **To protect all fields on a card:** If your goal is to protect the entire card, thereby preventing any updates to it, add the tag `AnkiCollab_Protect::All`.

Additionally, it's worth noting that certain fields may already be protected by the maintainers on the website. This is often the case for "personal" fields designed for individual customization. If you're unsure whether a field is protected, or if you're a maintainer looking to protect fields for all users, you can check and adjust these settings in the Deck Settings on the website.
</details>
<details close>
<summary><b>I get this Error: "Please only use notetypes that the maintainer added"!?</b></summary>
You can only suggest notes with notetypes that already exist on the deck (since its initial upload). That way, the maintainer can be sure that subscribers only upload notes with correct notetypes. Allowing new notetypes will soon be a toggleable option on the website.
If you have a lot of subscribers, I'll manually help you but if it's just you and your friend, please reupload the deck with all notetypes configured properly.
</details>

## Maintainer

<details close>
<summary><b>How do I delete a deck?</b></summary>
You can delete a deck by going to the Manage Decks option on the side-bar -> select your deck in the page for the deck you are maintaining -> at the bottom of the page you will have the option to delete the deck
</details>

<details close>
<summary><b>How do I make a deck private?</b></summary>
You can make a deck private by going to the Manage Decks option on the side-bar -> select your deck in the page for the deck you are maintaining -> you will have a checkbox to make the deck private (unlisted) by simply clicking that checkbox the deck will be made private
</details>

<details close>
<summary><b>How do I put a description on the Deck Browser page on AnkiCollab?</b></summary>
When you initially publish a deck, the deck description is also uploaded and can be seen on AnkiCollab's deck browser page. If you want to change it, you can go to the Manage Decks option on the side-bar -> select your deck in the page for the deck you are maintaining -> you will have the option to Update deck description. keep in mind deck description is written using HTML (as is most things within anki) so you can style it accordingly
</details>

<details close>
<summary><b> How can I inform my Subscribers about changes?</b></summary>
This can be done by publishing a changelog message. To publish a changelog message you simply have to go to the Manage Decks option on the website side-bar -> select your deck in the page for the deck you are maintaining -> Add a changelog message as well as view your previous changelog messages (if you published any).
<br>
<img src="https://i.imgur.com/T4kiBYI.png" width="50%">
  
<br/><br/>
And subscribers will see a popup that lists all changes that occurred since they last updated:
<img src="https://i.imgur.com/mpzDCEB.png" width="50%">
</details>

<details close>
  <summary><b>How can I add new changelog messages from within the Anki Desktop App?"</b></summary>
Here's how it works:

1. **Log In and Open the Deck Browser**: Ensure that you are logged in to your AnkiCollab account from within the Anki Desktop app.

2. **Deck Selection**: To access this feature, you must be a maintainer of the deck. Right-click on the deck of your choice to open the context menu from the Deck Browser.

3. **"Add Changelog" Option**: After right-clicking on the deck, you'll notice a new option: "Add Changelog." Click on it to proceed.

4. **Update Your Changelog**: A user-friendly interface will appear, allowing you to enter your changelog details quickly and efficiently. Describe the changes, updates, and improvements you've made to the deck.

5. **Save and Share**: Once you're satisfied with the changelog, hit the "Publish" button to store your changes. Your changelog will be updated instantly on the website and synced with other users.

This new capability eliminates the need to navigate to the Anki website separately to manage your deck changelogs. Simplify your workflow and focus on creating outstanding study materials!
</details>

<details close>
<summary><b>How do I change the deck name on AnkiCollab?</b></summary>
The only way to do that currently is by messaging the Discord group with your request here: https://discord.com/invite/9x4DRxzqwM 
Keep in mind that when you change the deck name on AnkiCollab, you will also have to change your deck name locally. All your subscribers must also change their deck name locally to receive changes.
</details>

<details close>
<summary><b>How can I use Optional Tags on AnkiCollab?</b></summary>
Optional Tags are a cool way to make your Deck Tag Structure less crowded. Imagine your deck is used by 3 different schools and each school has the cards tagged according to their school curriculum. Not all subscribers want to have all these curriculums in their local collection because it makes it a lot less readable. 
To solve that issue, you can use Optional Tags! These tags are only synchronized to the users that subscribed to them.
<br /><br />
To create a new Optional Tag group, navigate to the AnkiCollab Website > Manage Decks > Select your Deck > Optional Tags: Show All.

<br />
This will bring you to a page like this:
<br />
<img src="https://cdn.discordapp.com/attachments/1066468817351483502/1102317974511177858/RynkViW.png" width="50%">

Here you can add the new Tag Groups you want to use (For example, one for each school).

After you've added these, go ahead and open Anki!

To classify a tag as an "optional tag" it needs the prefix <code>AnkiCollab_Optional::</code> followed by the tag group you just specified on the website.

An example tag could look like this:
<br />
<img src="https://i.imgur.com/aRknj1g.png" width="50%">

Now you can go ahead and tag your notes to your liking and the subscribers will see a popup like this, when they subscribe to your deck:
<br />
<img src="https://cdn.discordapp.com/attachments/1066468817351483502/1102317974846718072/ZGS1WNr.png" width="50%">

and if they choose to subscribe to the ASU Tag, they will find all the tagged cards in their collection, but none of the tags they haven't subscribed to!
<br />
<img src="https://i.imgur.com/L0vmXlP.png" width="50%"> 

Note that this only affects tags. Notes and Cards are unaffected by this. All subscribers will always get all cards no matter what tag they have.
</details>

<details close>
<summary><b>How do I add my friends as maintainers for my deck on AnkiCollab?</b></summary>
  
Go to the AnkiCollab website.
Click on the "Manage Decks" option in the side-bar.<br />
Select the deck you are maintaining from the deck page.<br />
On the left side, you will find a title that says "Maintainers" with a button that says "Show All". Click the "Show All" button.<br />
A list of current maintainers for the deck will be displayed, along with an input box to add the email of new maintainers.<br />
Ask your friend to create an account on AnkiCollab.com using the email they want to use as a maintainer.<br />
Add their email in the input box and click the green "Add" button.<br />
Once your friend logs in to the AnkiCollab website using their email, they will be able to see your deck and review changes.<br /><br />
  
  
Please note that maintainers do not have full access to your deck. There are certain actions, such as deleting the deck or adding more maintainers, that only you can perform.
  
![Screenshot from 2023-06-04 18-49-41](https://github.com/CravingCrates/AnkiCollab-Plugin/assets/70135155/2777a855-f387-4731-a443-264070d96312)
![Screenshot from 2023-06-04 19-04-49](https://github.com/CravingCrates/AnkiCollab-Plugin/assets/70135155/723a7182-308f-4ab1-9efa-debe7672936d)

  
</details>

If you have a question that hasn't been answered in this FAQ, please reach out to us on Discord and we will try our best to help you :)

