# Comments on the SRT Controller

- There should be separate Student and Maintainer modes. Switching between them should only be possible through the backend or, for convenience, a keyboard shortcut or password.
- The current interface should serve as the Maintainer view.
- In the Student view:
    - The "Update Firmware" and "Restart" controls should be hidden.
    - Network and Settings options should not be editable.
    - Pointing should only display the current offsets and an explanation. It should not allow model loading or offset entry. (Do we need offset entry at all?)
    - Help should be updated accordingly.
- The Scheduler button does not work.
- Why is "Pointing Calibration" included in Settings?
- Why is there a Horizon input in Settings? There is a much more sophisticated way to handle the horizon in the manager.
- I think the controller should only provide altitude and azimuth controls, rather than allowing users to slew to any target, as this is the simpler version.

# Comments on the SRT Manager

Overall, I think the site is fragmented across too many tabs, with overlapping functions. It could be reorganised into a cleaner, more intuitive interface.

I propose the following layered structure, with a clear purpose for each section:

- Observe / Schedule
- Analyse / Model
- Telescope (configuration, data, and status)
- Student Guide

## Observer / Scheduler

This tab should handle everything that happens before an observation is made, primarily the functions currently provided by the scheduler.

- Setup (+Add Observation):
    - Users should be able to schedule an observation, as they can now.
    - However, do we need "Load" and "Export"? Would we ever create an observation file outside the scheduler? Do we need to export observation settings if the site already saves them?
    - Do we need "Save Schedule"? Would it be more efficient to save schedules automatically and allow users to delete them when necessary? Especially that it seems autosave is already in place.
    - Scheduler should be capable of functions currently only the 'Observer' can do to control the SRT, like: immidiate start, simulator handoff.
- Past schedules:
    - "End" appears twice in the descriptions, it should say in their names what the difference is.
    - I would add an action that takes users to the results of a scheduled observation. This should only be available once the observation has been completed.
    - Showing "Expired" doesn't convey all the information about a past observation. Rather show Completed/Partial/Failed/Cancelled/Missed. 

## Analyser

This tab should be used after an observation has been made. It should combine functions from the current "Observe" and "Simulator" tabs.

- At the top, below the main menu, I would add two options: "Observed" and "Simulator", with "Observed" selected by default.
- The main change would be to remove the option to set up or start an observation here. That is the scheduler's role. I think it makes more sense to have a single place from which to start observations.
- The current Observer functions would therefore be redundant. Its only unique feature is the ability to specify what happens after an observation.
- The main purpose of this section should be to analyse past observations and to view running ones.
- The dropdown menu for choosing a system is not very clear. It does not display the full filename, and the assigned name is rarely visible.
- Interactions with the plots are also inconsistent. The order of the buttons changes depending on the type of observation selected.
- Why is there a "Plot Result" button? The results should be plotted automatically. "dB Scale" and "Fit Model" should be in the lower box, as they affect the plot. Only the PDF/file download controls should sit outside that box, preferably below the dropdown menu rather than beside it.
- The "PRESTO Fold" button should only appear when pulsars are selected. (I also do not understand what it does.)
- Using "Fit Model" should also show which model is being fitted.

Users should also be able to select the Simulator. This could remain largely as it is, with a few minor changes:

- Why can users change the site name?
- I get why users change the site's latitude and longitude, but there should be a set to SRT default option.
- I think an "Find nearby observations" feature would be useful. If I click somewhere in the simulator and a matching observation has already been made, this feature could show a dropdown containing those observations. Selecting one would take the user back to the Analyser.

## Telescope

This tab should provide information and calibration tools for the telescope, rather than for observations. It should combine the functions of RF Calibration, Camera, Configuration, Log, Horizon, and Sun Scan.

As with Analyse, selecting Telescope Information should display a second row of options:

- Pointing
- Horizon
- RF Calibration
- Camera
- Configuration
- Log

I think telescope information and settings should only be editable by maintainers. Access could be controlled through a password, a keyboard shortcut, or a backend change followed by a reboot. In general, students should be able to view Horizon, Pointing, RF Calibration, and Configuration, but should not be able to edit the default files or settings.

- Pointing (previously Sun Scan):
    - Sun Scan and Calibration Day should each have a heading explaining their purpose.
    - Sun Scan:
        - Why is a plot displayed? If we show a plot, I think we should make it clear what has been selected and plotted.
    - Calibration Day:
        - Why can users clear all data? I think this should only be possible in Maintainer mode or through the backend.
        - The pointing model should also display the current offsets.
        - Should Beam have its own section? (I am not sure what its purpose is.)
- Horizon:
    - I like the main interface a lot.
    - Why is the left column so long?
    - I am not sure what the surveys are for.
- RF Calibration:
    - I like this section. I would just add a heading explaining the page's functions and purpose.
- Camera, Configuration, and Log:
    - No comments here. :)

## Student Guide

I think this is great. The chapters are easy to digest and clearly written, and the guide leads users through the material in a natural sequence.
