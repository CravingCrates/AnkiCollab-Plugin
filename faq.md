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
<summary><b>I changed the name of my deck/subdeck locally and now I get the error "No local deck ID"</b></summary>
Deck names should correlate exactly to the name that is on AnkiCollab. Currently there is no way to change your local deck name and continue publishing/receiving changes. Watch out for future changes though ;)
</details>

## Subscriber

<details close>
<summary><b>Can I suggest a card to be deleted?</b></summary>
Currently, there is no way to delete individual cards from a deck uploaded to <a href="https://ankicollab.com/decks">AnkiCollab.com/Decks</a>. As a workaround, suggest a tag like <code>#!DELETE</code> indicating that you want a card deleted.
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
<summary><b>How do I change the deck name on AnkiCollab?</b></summary>
The only way to do that currently is by messaging the Discord group with your request here: https://discord.com/invite/9x4DRxzqwM 
Keep in mind that when you change the deck name on AnkiCollab, you will also have to change your deck name locally. All your subscribers must also change their deck name locally to receive changes.
</details>

<details close>
<summary><b>How can I use Optional Tags on AnkiCollab?</b></summary>
Optional Tags are a cool way to make your Deck Tagstructure less crowded. Imagine your deck is used by 3 different schools and each school has the cards tagged according to their school curriculum. Not all subscribers want to have all these curriculums in their local collection because it makes it a lot less readable. 
To solve that issue, you can use Optional Tags! These tags are only synchronized to the users that subscribed to them.
<br /><br />
To create a new Optional Tag group, navigate to the AnkiCollab.com Website > Manage Decks > Select your Deck > Optional Tags: Show All.

<br />
This will bring you to a page like this:
<img src="https://cdn.discordapp.com/attachments/1066468817351483502/1102317974511177858/RynkViW.png" width="50%">

Here you can add the new Tag Groups you want to use.

After you've added it, go ahead and open Anki!

To classify a tag as a "optional tag" it needs the prefix <code>AnkiCollab_Optional::</code> followed by the tag group you just specified on the website.

A example tag could look like this:
<br />
<img src="https://i.imgur.com/aRknj1g.png" width="50%">

Now you can go ahead and tag your notes to your liking and the subscribers will see a popup like this, when they subscribe to your deck:
<br />
<img src="https://cdn.discordapp.com/attachments/1066468817351483502/1102317974846718072/ZGS1WNr.png" width="50%">

and if they choose to subscribe to the ASU Tag, they will find all the tagged cards according to your structure in their collection, but none of the tags they haven't subscribed to!
<br />
<img src="https://i.imgur.com/L0vmXlP.png" width="50%"> 

Note that this only affects tags. Notes and Cards are unaffected by this. All subscribers will always get all cards no matter what tag they have.
</details>

If you have a question that hasn't been answered in this FAQ, please reach out to us on Discord and we will try our best to help you :)

