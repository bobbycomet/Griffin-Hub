## Griffin Hub 1.1.0 is being worked on. What to expect: Griffin Play, this is a feature being added. What was originally going to be a standalone tool, will now be included in Griffin Hub. Griffin Play is essentially a ProtonDB tool (no affiliation). 

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

The Griffin Linux project is moving from individual applications to a more unified front. While you do not need every application, having them all will be beneficial to some workflows. This is why the state.json is so important. It is automatically created with Kernel Autotune, and from there, any application you choose in the ecosystem benefits.
