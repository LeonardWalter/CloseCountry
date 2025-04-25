const APP_VERSION = "dev-local";
console.log("Closer Country App Version:", APP_VERSION);

document.addEventListener('DOMContentLoaded', () => {
    const themeToggleButton = document.getElementById('theme-toggle');
    const bodyElement = document.body;

    const baseCountryEl = document.getElementById('base-country');
    const choice1Button = document.getElementById('choice1');
    const choice2Button = document.getElementById('choice2');
    const currentScoreEl = document.getElementById('current-score');
    const highScoreEl = document.getElementById('high-score');
    const gameOverEl = document.getElementById('game-over');
    const finalScoreEl = document.getElementById('final-score');
    const playAgainButton = document.getElementById('play-again');
    const gameAreaEl = document.getElementById('game-area');
    const showMapLink = document.getElementById('show-map-link');
    const leafletMapContainer = document.getElementById('leaflet-map'); // Container for Leaflet map
    const feedbackBoxEl = document.getElementById('feedback-box');       // Persistent feedback box
    const feedbackTextEl = document.getElementById('feedback-text');     // Text inside feedback box
    const choice1Img = choice1Button.querySelector('.choice-flag');      // Img tag in button 1
    const choice1Label = choice1Button.querySelector('.country-name-label'); // Span tag in button 1
    const choice2Img = choice2Button.querySelector('.choice-flag');      // Img tag in button 2
    const choice2Label = choice2Button.querySelector('.country-name-label'); // Span tag in button 2
    const nicknameArea = document.getElementById('nickname-area');
    const nicknameInput = document.getElementById('nickname-input');
    const submitNicknameBtn = document.getElementById('submit-nickname-btn');
    const nicknameFeedback = document.getElementById('nickname-feedback');
    const leaderboardSection = document.getElementById('leaderboard-section');
    const leaderboardDisplay = document.getElementById('leaderboard-display');

    let currentBaseCountryName = '';
    let currentTarget1Name = '';
    let currentTarget2Name = '';
    let mapParams = null; // Stores {base, t1, t2} for map request
    let map = null; // Holds the Leaflet map instance
    let isFetchingRound = false;

    const sunIcon = '☀️';
    const moonIcon = '🌙';

    function applyTheme(theme) {
        if (theme === 'dark') {
            bodyElement.classList.add('dark-mode');
            themeToggleButton.textContent = sunIcon; // Show sun icon when dark
            localStorage.setItem('theme', 'dark');
        } else {
            bodyElement.classList.remove('dark-mode');
            themeToggleButton.textContent = moonIcon; // Show moon icon when light
            localStorage.setItem('theme', 'light');
        }
    }

    // Check initial theme on load
    const savedTheme = localStorage.getItem('theme');
    const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;

    if (savedTheme) {
        applyTheme(savedTheme);
    } else {
        applyTheme(prefersDark ? 'dark' : 'light');
    }

    themeToggleButton.addEventListener('click', () => {
        const isDarkMode = bodyElement.classList.contains('dark-mode');
        applyTheme(isDarkMode ? 'light' : 'dark');
    });

    // Function to get the correct static path (handles /game prefix)
    function getStaticPath(relativePath) {
        const runningUnderSubpath = window.location.pathname.startsWith('/game');
        const prefix = runningUnderSubpath ? '/game' : '';
        return `${prefix}/static/${relativePath}`;
    }

    // Disables/Enables choice buttons and play again button
    function setLoadingState(isLoading) {
        choice1Button.disabled = isLoading;
        choice2Button.disabled = isLoading;
        playAgainButton.disabled = isLoading;
    }

    function displayRoundData(data) {
        if (!data || data.error || data.game_over) {
           console.error("Invalid data passed to displayRoundData:", data);
           feedbackTextEl.textContent = data?.error || data?.message || 'Failed to display round data.';
           feedbackBoxEl.className = 'incorrect';
           feedbackBoxEl.style.display = 'block';
           gameAreaEl.style.display = 'none';
           return;
       }

       currentBaseCountryName = data.base_country.name;
       currentTarget1Name = data.target1.name;
       currentTarget2Name = data.target2.name;

       baseCountryEl.textContent = currentBaseCountryName;
       choice1Button.dataset.country = currentTarget1Name;
       choice1Label.textContent = currentTarget1Name;
       currentScoreEl.textContent = data.score;
       if (data.target1.code) {
           choice1Img.src = getStaticPath(`svg/${data.target1.code.toLowerCase()}.svg`);
           choice1Img.alt = `Flag of ${currentTarget1Name}`;
           choice1Img.onerror = () => { choice1Img.style.display = 'none'; choice1Label.textContent = `${currentTarget1Name} (?)`;};
           choice1Img.style.display = 'inline-block';
       } else { choice1Img.src = ''; choice1Img.style.display = 'none'; }

       choice2Button.dataset.country = currentTarget2Name;
       choice2Label.textContent = currentTarget2Name;
       if (data.target2.code) {
           choice2Img.src = getStaticPath(`svg/${data.target2.code.toLowerCase()}.svg`);
           choice2Img.alt = `Flag of ${currentTarget2Name}`;
           choice2Img.onerror = () => { choice2Img.style.display = 'none'; choice2Label.textContent = `${currentTarget2Name} (?)`;};
           choice2Img.style.display = 'inline-block';
       } else { choice2Img.src = ''; choice2Img.style.display = 'none'; }
   }

    function prefetchSvgs(codes) {
        if (!Array.isArray(codes)) return;
        console.log("Prefetching SVGs for codes:", codes); // DEBUG
        codes.forEach(code => {
            if (code && typeof code === 'string') {
                try {
                    const img = new Image();
                    img.src = getStaticPath(`svg/${code.toLowerCase()}.svg`);
                } catch (svgError) {
                    console.error(`Error initiating SVG prefetch for code ${code}:`, svgError);
                }
            }
        });
    }

    async function fetchNewRound() {
        if (isFetchingRound) return;
        isFetchingRound = true;
    
        if(feedbackBoxEl.className == 'incorrect' || feedbackBoxEl.className == 'correct') { // Hide feedback on new round
            feedbackBoxEl.style.display = 'none';
            feedbackTextEl.textContent = '';
            feedbackBoxEl.className = '';
        }
        // ... (rest of UI cleanup: leaderboard, nickname, map) ...
        leaderboardSection.style.display = 'none';
        nicknameArea.style.display = 'none';
        leafletMapContainer.style.display = 'none';
        leafletMapContainer.innerHTML = '';
        if (map) { map.remove(); map = null; }
        showMapLink.style.display = 'none';
        gameOverEl.style.display = 'none';
        gameAreaEl.style.display = 'block';
        setLoadingState(true);
    
        try {
            const response = await fetch('start_round'); // Always fetch
            if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
            const data = await response.json();

            if (data.error || data.game_over) {
                console.error("Error/Game Over received during fetch:", data.error || data.message);
                feedbackTextEl.textContent = data?.error || data?.message || 'Failed to start round.';
                feedbackBoxEl.className = 'incorrect'; // Use incorrect style for errors/game over message
                feedbackBoxEl.style.display = 'block';
                gameAreaEl.style.display = 'none';
                gameOverEl.style.display = 'block';
                handleGameOver(data); // Process game over data
            } else {
                console.log("Fetch successful, displaying data for:", data.base_country.name); // DEBUG
                displayRoundData(data);
                const codesToPrefetch = [
                    data.next_target1_code, // Use the specific codes for next choices
                    data.next_target2_code,
                    data.base_country?.code // Prefetch current base flag too as it's shown now
                ].filter(Boolean); // Filter out any null/undefined codes
                prefetchSvgs(codesToPrefetch);
            }
        } catch (error) {
            console.error('Error fetching new round:', error);
            feedbackTextEl.textContent = 'Error loading game round. Please try refreshing.';
            feedbackBoxEl.className = 'incorrect';
            feedbackBoxEl.style.display = 'block';
            gameAreaEl.style.display = 'none';
        } finally {
            setLoadingState(false);
            isFetchingRound = false;
        }
    }

    // Sends the player's guess to the backend
    async function makeGuess(chosenCountryName) {
        if (isFetchingRound) { console.log("Guess skipped: round is loading."); return; }
        const otherCountryName = (chosenCountryName === currentTarget1Name) ? currentTarget2Name : currentTarget1Name;
        setLoadingState(true); // Disable buttons while checking

        try {
            const response = await fetch('make_guess', {
                method: 'POST',
                headers: {'Content-Type': 'application/json',},
                body: JSON.stringify({
                    base_country_name: currentBaseCountryName,
                    chosen_country_name: chosenCountryName,
                    other_country_name: otherCountryName
                }),
            });

            if (!response.ok) {
                const errorData = await response.json().catch(() => ({ error: 'Unknown server error' }));
                throw new Error(errorData.error || `HTTP error! status: ${response.status}`);
            }
            const result = await response.json();

            // Construct feedback message with newlines
            let feedbackPrefix = "";
            let feedbackClass = "";
            const distString1 = `Distance to ${chosenCountryName}: ${result.chosen_dist} km`;
            const distString2 = `Distance to ${otherCountryName}: ${result.other_dist} km`;

            if (result.correct) {
                feedbackPrefix = `CORRECT! ${currentBaseCountryName} is closer to ${result.closer_country}.`;
                feedbackClass = 'correct';
                currentScoreEl.textContent = result.score;
                if (result.new_highscore) {
                    highScoreEl.textContent = result.highscore;
                    feedbackPrefix += "\n✨ New High Score! ✨";
                }

                const finalFeedbackText = `${feedbackPrefix}\n${distString1}\n${distString2}`;
                feedbackTextEl.textContent = finalFeedbackText;
                feedbackBoxEl.className = feedbackClass;
                feedbackBoxEl.style.display = 'block';
                fetchNewRound();
            } else {
                feedbackPrefix = `WRONG! ${currentBaseCountryName} is closer to  ${result.closer_country}.`;
                feedbackClass = 'incorrect';

                const finalFeedbackText = `${feedbackPrefix}\n${distString1}\n${distString2}`;
                feedbackTextEl.textContent = finalFeedbackText;
                feedbackBoxEl.className = feedbackClass;
                feedbackBoxEl.style.display = 'block';

                handleGameOver(result);
                setLoadingState(false);
            }
        } catch (error) {
            feedbackTextEl.textContent = `Error: ${error.message}.\nPlease try again or refresh.`;
            feedbackBoxEl.className = 'incorrect';
            feedbackBoxEl.style.display = 'block';
            setLoadingState(false); // Re-enable buttons on error
        }
    }

    // Handles the UI changes when the game ends
    function handleGameOver(result) {
        gameAreaEl.style.display = 'none'; // Hide game area
        gameOverEl.style.display = 'block'; // Show game over screen
        finalScoreEl.textContent = result.final_score;
        highScoreEl.textContent = result.highscore; // Ensure high score is up-to-date

        leafletMapContainer.style.display = 'none'; // Keep it hidden until link is clicked
        leafletMapContainer.innerHTML = '';      // Clear any old map/loading text
        if (map) { map.remove(); map = null; }

        if (result.prompt_nickname) {
            if (result.existing_nickname) {
                nicknameInput.value = result.existing_nickname; // Set value from backend
            } else {
                nicknameInput.value = ''; // Clear if no existing nickname
            }
            nicknameFeedback.textContent = '';
            nicknameArea.style.display = 'block';
            submitNicknameBtn.disabled = false;
        } else {
            nicknameArea.style.display = 'none'; // Hide if not a new high score
        }

        // Show map link if applicable
        if (result.map_available && result.map_params) {
            mapParams = result.map_params;
            showMapLink.style.display = 'inline';
        } else {
            showMapLink.style.display = 'none';
            mapParams = null;
        }
        fetchAndDisplayLeaderboard()
    }

    // Fetches GeoJSON data and displays the Leaflet map
    async function fetchAndShowMap() {
        if (!mapParams || !mapParams.base || !mapParams.t1 || !mapParams.t2) {
            console.error("Map parameters missing.");
            leafletMapContainer.innerHTML = '<p>Cannot load map: parameters missing.</p>';
            return;
        }
        leafletMapContainer.style.display = 'block';
        leafletMapContainer.innerHTML = '<p>Loading map data...</p>'; // Show loading indicator
        showMapLink.style.display = 'none'; // Hide link after click

        const queryParams = new URLSearchParams({ base: mapParams.base, t1: mapParams.t1, t2: mapParams.t2 });
        const mapDataUrl = `get_game_over_data?${queryParams.toString()}`; // Relative path

        try {
            const response = await fetch(mapDataUrl);
            if (!response.ok) {
                let errorText = `Failed to load map data (Status: ${response.status})`;
                try { const errData = await response.text(); if (errData) errorText += `: ${errData}`; } catch (e) {}
                throw new Error(errorText);
            }
            const geojsonData = await response.json();

            if (!geojsonData || geojsonData.type !== 'FeatureCollection' || !geojsonData.features) {
                throw new Error("Invalid GeoJSON data received from server.");
            }

            leafletMapContainer.innerHTML = '';
            if (map) { map.remove(); map = null; }
            map = L.map(leafletMapContainer).setView([0, 0], 2); // Intial world view

            L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png', {
                attribution: '© <a href="https://www.openstreetmap.org/copyright">OSM</a> © <a href="https://carto.com/attributions">CARTO</a>',
                subdomains: 'abcd', maxZoom: 19
            }).addTo(map);

            // Add GeoJSON Layer from fetched data
            const geoJsonLayer = L.geoJSON(geojsonData, {
                style: function (feature) {
                    switch (feature.properties.feature_type) {
                        case 'country_shape': return { fillColor: feature.properties.color || 'grey', weight: 1, opacity: 1, color: 'white', fillOpacity: 0.6 };
                        case 'distance_line': return { color: '#333', weight: 2, opacity: 0.8, dashArray: '5, 5' };
                        default: return {};
                    }
                },
                pointToLayer: function (feature, latlng) {
                    if (feature.properties.feature_type === 'point') {
                        return L.circleMarker(latlng, { radius: 6, fillColor: "#ff7800", color: "#000", weight: 1, opacity: 1, fillOpacity: 0.8 });
                    }
                },
                onEachFeature: function (feature, layer) {
                    let popupContent = "";
                    const props = feature.properties;
                    if (props) {
                        if (props.name) popupContent += `<b>${props.name}</b>`;
                        if (props.distance_km) popupContent += `<br>Distance: ${props.distance_km} km`;
                        if (props.feature_type === 'distance_line' && props.distance_km != null) {
                            content = `${props.distance_km.toLocaleString(undefined, {maximumFractionDigits: 0})} km`; // Format number
                            layer.bindTooltip(content, {
                                permanent: true,      // Make it always visible
                                direction: 'center',  // Position near the center of the line
                                className: 'distance-label-tooltip', // Add custom CSS class
                            });
                        }
                    }
                    if (popupContent) layer.bindPopup(popupContent);
                }
            }).addTo(map);

            try {
                map.fitBounds(geoJsonLayer.getBounds().pad(0.1));
            } catch(e) {
                console.warn("Could not fit map bounds automatically.", e);
            }
        } catch (error) {
            console.error("Error fetching or displaying map:", error);
            leafletMapContainer.innerHTML = `<p style="color: var(--incorrect-color);">Sorry, could not load the map. ${error.message || ''}</p>`;
        }
    }

    async function fetchAndDisplayLeaderboard() {
        leaderboardSection.style.display = 'block'; // Show leaderboard area
        leaderboardDisplay.innerHTML = '<p>Loading...</p>';
        try {
            const response = await fetch('get_leaderboard');
            if (!response.ok) throw new Error('Failed to fetch leaderboard');
            const data = await response.json();

            if (data.leaderboard && data.leaderboard.length > 0) {
                let leaderboardHTML = '<ol>';
                data.leaderboard.forEach((entry) => {
                    leaderboardHTML += `<li> ${escapeHTML(entry.nickname || 'Anonymous')} - ${entry.score}</li>`;
                });
                leaderboardHTML += '</ol>';
                leaderboardDisplay.innerHTML = leaderboardHTML;
            } else {
                leaderboardDisplay.innerHTML = '<p>No scores yet. Be the first!</p>';
            }
        } catch (error) {
            console.error("Error fetching leaderboard:", error);
            leaderboardDisplay.innerHTML = '<p>Could not load leaderboard.</p>';
        }
    }
     // Helper to prevent basic XSS from nicknames
    function escapeHTML(str) {
        const div = document.createElement('div');
        div.appendChild(document.createTextNode(str));
        return div.innerHTML;
    }

    submitNicknameBtn.addEventListener('click', async () => {
        const nickname = nicknameInput.value.trim();
        if (!nickname) {
            nicknameFeedback.textContent = 'Please enter a nickname.';
            nicknameFeedback.style.color = 'var(--incorrect-color)';
            return;
        }

        submitNicknameBtn.disabled = true;
        nicknameFeedback.textContent = 'Submitting...';
        nicknameFeedback.style.color = 'var(--text-muted-color)';

        try {
            const response = await fetch('submit_nickname', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ nickname: nickname })
            });

            if (!response.ok) {
                const errData = await response.json().catch(() => ({error: 'Unknown error'}));
                throw new Error(errData.error || 'Failed to submit score.');
            }

            const result = await response.json();
            if (result.success) {
                nicknameFeedback.textContent = 'Score submitted!';
                nicknameFeedback.style.color = 'var(--correct-color)';
                nicknameArea.style.display = 'none';
                if(result.leaderboard) {
                    updateLeaderboardUI(result.leaderboard);
                }
            } else {
                throw new Error(result.error || 'Failed to submit score.');
            }

        } catch (error) {
            console.error("Error submitting nickname:", error);
            nicknameFeedback.textContent = `Error: ${error.message}`;
            nicknameFeedback.style.color = 'var(--incorrect-color)';
            submitNicknameBtn.disabled = false;
        }
    });

    function updateLeaderboardUI(leaderboardData) {
        if (leaderboardData && leaderboardData.length > 0) {
            let leaderboardHTML = '<ol>';
            leaderboardData.forEach((entry, index) => {
                leaderboardHTML += `<li>${escapeHTML(entry.nickname || 'Anonymous')} - ${entry.score}</li>`;
            });
            leaderboardHTML += '</ol>';
            leaderboardDisplay.innerHTML = leaderboardHTML;
        } else {
            leaderboardDisplay.innerHTML = '<p>No scores yet. Be the first!</p>';
        }
    }

    choice1Button.addEventListener('click', () => makeGuess(choice1Button.dataset.country));
    choice2Button.addEventListener('click', () => makeGuess(choice2Button.dataset.country));
    showMapLink.addEventListener('click', (e) => { e.preventDefault(); fetchAndShowMap(); });
    playAgainButton.addEventListener('click', () => { fetchNewRound(); });

    fetchNewRound();
});
