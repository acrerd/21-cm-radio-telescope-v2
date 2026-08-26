// The Simulator tab, an iframe onto the separate web simulator.
//
// Part of the operator page, split out of one 2144-line file on 2026-08-25.
// Loaded as a classic script: everything here shares one global scope,
// exactly as it did before the split.

        // With a url, (re)load the simulator at that deep link - the per-entry
        // button on the Scheduler tab; without one, the plain simulator, once.
        function showSimulator(url) {
            if (simFrame) {
                if (url) simFrame.src = url;
                return;
            }
            const host = document.getElementById('simHost');
            host.innerHTML = '';
            simFrame = document.createElement('iframe');
            simFrame.src = url || '/simulator/';
            simFrame.style.cssText = 'width:100%; height:100%; border:0; display:block;';
            simFrame.title = 'Sky simulator';
            host.appendChild(simFrame);
        }
