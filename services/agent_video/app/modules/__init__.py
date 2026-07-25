"""The four internal modules of the Video Agent — storyboard, voiceover,
assembly, thumbnail — kept as separate classes/files for readability and
because each will grow its own real provider integration and dependency
footprint, but invoked from a single `VideoAgent.run()` (see
../video_agent.py) rather than as four separately-dispatched jobs.
"""
