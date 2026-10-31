## Griffin Hub 1.1.0 is being worked on. What to expect: Griffin Play, this is a feature being added. What was originally going to be a standalone tool, will now be included in Griffin Hub. Griffin Play is essentially a ProtonDB tool (no affiliation). Branding image will also change to have the Grix mascot for better brand cohesion.

### How it works: 

- A game needs a Proton version, GE, or maybe dependencies for the system as a whole. This allows you to lock that compatibility in, while also using ProtonDB's user reports to give you the option to either copy-paste the launch command, or to have Geiffin play add it for you. The list will be curated to around 390 games (with contributions open for the community to add more).
- Update user reports based on your hardware, kernel, and distro to give you the best option, or the closest comparable one.
- SteamDeck support, OpenSuse support, and Arch support within the tool. Other distro support will be via plugins.

### Other features to be added:

- The Process Sentry Daemon and Kernel Autotune scripts integrated into Griffin Hub to be installed on your system, with multiple init systems support
- Upgraded Graphs and charts
- Steam wrappers moved to advanced settings

## What does this mean for the rest of the Griffin applications?

- state.json becomes the norm. This holds all of your system's hardware, system configs, and kernel information, so each application will be able to utilize this for better synching.
- Most other applications in the parent CUXL umbrella are already mostly multi-distro, so it is only a Griffin architecture shift.
- Emphasis on SentryV3 and Kernel Autotune stabilization for multi distro general performance, gaming performance, an production performance profiles.
- Griffin Persona profiles becoming the norm. Personas are the workflows that you choose for your PC and update the state.json, and update Kernel Autotune and SentryV3 based on those workflows, or combination of workflows. This allows your system to classify itself as a workstation, gaming PC, general PC, streamer, Vtuber, etc. This means Grix can diagnose your system based on those workflows, Griffin Hub can be tailored to your gaming system, Fan Hub has more useful information for fan curves, XKM can suggest proper kernels, etc.

### **New addition: 10/30/2026:** Griffin Game tweaks

## What is Game Tweaks?

Game Tweaks combines concepts from the abandoned project WinBridge and the CUXL application Kalium to make prefixes for Windows games as standalone prefixes, or made into a non-steam game.

- Browse for the game's launcher .exe file
- Set the game up
- Choose simple or advanced settings
- Choose Proton version to be used
- Choose Windows 10/11 compatibility layer
- Choose launch settings
- 3 Winetricks verb configs: Minimal, Standard, Extended

## How is this different from WinBridge? It sounds similar.

- WinBridge originally was too broad, and was trying to cover applications as well as games.
- The idea is similar, but the focus changed, and the way it gets there changed.

## The Kalium inspiration

Kalium automates games for MO2 by making MO2 instances a non-steam game. Repurposing the logic for Game Tweaks Is what makes this work. It follows the exact formula of automation.

## Will Kalium come to Griffin Hub, or will WinBridge be revived?

No to both.

- Kalium is highly specialized, while there are many Bethesda game modders out there, with the combination of Linux, we are talking about a niche within a niche. It is best as its own tool.
- WinBridge had way too much of a surface area to be maintained when I have so many tools and projects. It is possible to use Game Tweaks to get the same result, but no promises on that.

The Griffin Linux project is moving from individual applications to a more unified front. While you do not need every application, having them all will be beneficial to some workflows. This is why the state.json is so important. It is automatically created with Kernel Autotune, and from there, any application you choose in the ecosystem benefits.
