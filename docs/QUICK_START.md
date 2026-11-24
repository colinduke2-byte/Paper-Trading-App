# PaperDesk quick start

PaperDesk is a paper trading simulator: pretend money, no real orders, simulated prices by default.
This page takes you from nothing to a first paper trade on Windows, then to your phone. The full guide
is [USER_GUIDE.md](USER_GUIDE.md).

## From nothing to a first trade (Windows)

1. **Install Python.** Download the latest Python 3 from <https://www.python.org/downloads/windows/>.
   Run the installer, **tick "Add python.exe to PATH"**, and choose **Install Now**.
2. **Get PaperDesk.** On the PaperDesk page on GitHub press **Code > Download ZIP**. Right-click the ZIP
   in Downloads, **Extract All**, and choose a folder you will keep, such as `C:\PaperDesk`.
3. **Start it.** Open that folder and double-click **`run.bat`**. A black window opens. The first time it
   sets itself up for about a minute (internet needed). When it says
   `PaperDesk is starting on http://127.0.0.1:8000`, your browser opens PaperDesk. If it does not, type
   `http://127.0.0.1:8000` in a browser. Leave the black window open: PaperDesk runs only while it is open.
4. **Pick a symbol.** The yellow banner says "Paper trading · no real money · simulated market data".
   The watchlist on the left lists fictional companies. Click **MKTX**.
5. **Place a trade.** In the **Order ticket** on the right: keep **Buy**, type `10` in **Shares**, keep
   **Market**, and press **Review: Buy**. Check the estimate, then press **Place paper order**. A message
   confirms the fill.
6. **See the result.** Under **Positions** (bottom) you hold 10 MKTX. To sell, press **Close…** on that row,
   then **Review** and **Place paper order** again. **Fills**, **Orders** and **Reports** show the history.

To stop: press **Ctrl+C** in the black window. To start again: double-click `run.bat`. Your accounts are
saved in `backend\data`; copy that folder to back them up.

The **Help** button (top right) has a short in-app guide.

## Phone access in five steps

Use this only on your own home Wi-Fi. The connection is not encrypted. Use a passcode you use nowhere
else.

1. **Make the settings file.** In the PaperDesk folder, copy `.env.example` and rename the copy to
   `.env`. Open it with Notepad.
2. **Set a passcode.** Add (or un-comment) these two lines, with your own passcode in the single quotes
   (at least 8 characters):
   ```
   PAPERDESK_LAN=1
   PAPERDESK_PASSCODE='your own long passcode here'
   ```
   Save and close.
3. **Restart.** Press Ctrl+C in the black window, then double-click `run.bat`. When Windows asks about the
   firewall, tick **Private networks only** and press **Allow access**.
4. **Find the address.** The black window prints `On your phone, open this address:` followed by something
   like `http://192.168.1.20:8000`.
5. **Open it on the phone.** With the phone on the same Wi-Fi, type that address in the phone's browser,
   enter the passcode, and trade. To leave, open the menu (top right) and press **Sign out**.

If it does not connect, see "If the phone cannot connect" in section 12 of the user guide.

## Real prices, optionally

Sign up for a free Alpaca paper account, put its two keys in `.env`, and restart. Orders stay simulated.
The user guide (section 13) has the steps and the check command.
