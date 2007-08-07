# -*- Mode: Python -*-
# vi:si:et:sw=4:sts=4:ts=4
#
# Flumotion - a streaming media server
# Copyright (C) 2004,2005,2006,2007 Fluendo, S.L. (www.fluendo.com).
# All rights reserved.

# This file may be distributed and/or modified under the terms of
# the GNU General Public License version 2 as published by
# the Free Software Foundation.
# This file is distributed without any warranty; without even the implied
# warranty of merchantability or fitness for a particular purpose.
# See "LICENSE.GPL" in the source distribution for more information.

# Licensees having purchased or holding a valid Flumotion Advanced
# Streaming Server license may use this file in accordance with the
# Flumotion Advanced Streaming Server Commercial License Agreement.
# See "LICENSE.Flumotion" in the source distribution for more information.

# Headers in this file shall remain intact.

from flumotion.component import feedcomponent
from flumotion.common import errors, messages
from flumotion.common.planet import moods
from twisted.internet import defer
import threading
from flumotion.common.messages import N_
T_ = messages.gettexter('flumotion')
import gst

try:
    # icalendar and dateutil modules needed for scheduling recordings
    from icalendar import Calendar
    from dateutil import rrule
    HAVE_ICAL = True
except:
    HAVE_ICAL = False

class SwitchMedium(feedcomponent.FeedComponentMedium):
    def remote_switchToMaster(self):
        return self.comp.switch_to("master")
    
    def remote_switchToBackup(self):
        return self.comp.switch_to("backup")

class Switch(feedcomponent.MultiInputParseLaunchComponent):
    logCategory = 'comb-switch'
    componentMediumClass = SwitchMedium

    def init(self):
        self.uiState.addKey("active-eater")
        self.icalScheduler = None
        # _idealEater is used to determine what the ideal eater at the current
        # time is.
        self._idealEater = "master"
        # these deferreds will fire when relevant eaters are ready
        # usually these will be None, but when a scheduled switch
        # was requested and the eater wasn't ready, it'll fire when ready
        # so the switch can be made
        self._eaterReadyDefers = { "master": None, "backup": None }
        self._started = False

    def do_check(self):
        self.debug("checking whether switch element exists")
        from flumotion.worker.checks import check
        d = check.checkPlugin('switch', 'gst-plugins-bad')
        def cb(result):
            for m in result.messages:
                self.addMessage(m)
            # if we have been passed an ical file to use for scheduling
            # then start the ical monitor
            props = self.config['properties']
            icalfn = props.get('ical-schedule')
            if icalfn:
                if HAVE_ICAL:
                    try:
                        from flumotion.component.base import scheduler
                        self.icalScheduler = scheduler.ICalScheduler(open(
                            icalfn, 'r'))
                        self.icalScheduler.subscribe(self.eventStarted,
                            self.eventStopped)
                        if self.icalScheduler.getCurrentEvents():
                            self._idealEater = "backup"
                    except ValueError:
                        m = messages.Warning(T_(N_(
                            "Error parsing ical file %s, so not scheduling any"
                            " events." % icalfn)), id="error-parsing-ical")
                        self.addMessage(m)
                else:
                    warnStr = "An ical file has been specified for " \
                              "scheduling but the necessary modules " \
                              "dateutil and/or icalendar are not installed"
                    self.warning(warnStr)
                    m = messages.Warning(T_(N_(warnStr)), 
                        id="error-parsing-ical")
                    self.addMessage(m)
            return result
        d.addCallback(cb)
        return d
        
    def switch_to(self, eaterSubstring):
        raise errors.NotImplementedError('subclasses should implement '
                                         'switch_to')

    def is_active(self, eaterSubstring):
        # eaterSubstring is "master" or "backup"
        for eaterFeedId in self._inactiveEaters:
            eaterName = self.get_eater_name_for_feed_id(eaterFeedId)
            if eaterSubstring in eaterName:
                self.log("eater %s inactive", eaterName)
                return False
        return True

    # if an event starts, semantics are to switch to backup
    # if an event stops, semantics are to switch to master
    def eventStarted(self, event):
        self.debug("event started %r", event)
        if self.pipeline:
            self.switch_to_for_event("backup", True)

    def eventStopped(self, event):
        self.debug("event stopped %r", event)
        if self.pipeline:
            self.switch_to_for_event("master", False)

    def do_pipeline_playing(self):
        feedcomponent.MultiInputParseLaunchComponent.do_pipeline_playing(self)
        # needed to stop the flapping between master and backup on startup
        # in the watchdogs if the starting state is backup
        self._started = True

    def eaterSetActive(self, feedId):
        # need to just set _started to True if False and mood is happy
        feedcomponent.MultiInputParseLaunchComponent.eaterSetActive(self, feedId)
        if not self._started and moods.get(self.getMood()) == moods.happy:
            self._started = True

    def switch_to_for_event(self, eaterSubstring, startOrStop):
        """
        @param eaterSubstring: either "master" or "backup"
        @param startOrStop: True if start of event, False if stop
        """
        if eaterSubstring != "master" and eaterSubstring != "backup":
            self.warning("switch_to_for_event should be called with 'master'"
                " or 'backup'")
            return None
        self._idealEater = eaterSubstring
        d = defer.maybeDeferred(self.switch_to, eaterSubstring)
        def switch_to_cb(res):
            if not res :
                startOrStopStr = "stopped"
                if startOrStop:
                    startOrStopStr = "started"
                warnStr = "Event %s but could not switch to %s" \
                    ", will switch when %s is back" % (startOrStopStr,
                    eaterSubstring, eaterSubstring)
                self.warning(warnStr)
                m = messages.Warning(T_(N_(warnStr)), 
                        id="error-scheduling-event")
                self.addMessage(m)
                self._eaterReadyDefers[eaterSubstring] = defer.Deferred()
                self._eaterReadyDefers[eaterSubstring].addCallback(
                    lambda x: self.switch_to(eaterSubstring))
                otherEater = "backup"
                if eaterSubstring == "backup":
                    otherEater = "master"
                self._eaterReadyDefers[otherEater] = None
        d.addCallback(switch_to_cb)
        return d

class SingleSwitch(Switch):
    logCategory = "comb-single-switch"

    def init(self):
        Switch.init(self)
        self.switchElement = None
        # eater name -> name of sink pad on switch element
        self.switchPads = {}

    def get_pipeline_string(self, properties):
        eaters = self.eater_names

        pipeline = "switch name=switch ! " \
            "identity silent=true single-segment=true name=iden "
        for eater in eaters:
            tmpl = '@ eater:%s @ ! switch. '
            pipeline += tmpl % eater

        pipeline += 'iden.'

        return pipeline

    def configure_pipeline(self, pipeline, properties):
        self.switchElement = sw = pipeline.get_by_name("switch")
        # figure out the pads connected for the eaters
        padPeers = {} # padName -> peer element name
        for sinkPadNumber in range(0, len(self.eater_names)):
            self.debug("sink pad %d %r", sinkPadNumber, sw.get_pad("sink%d" % sinkPadNumber))
            self.debug("peer pad %r", sw.get_pad("sink%d" % (
                sinkPadNumber)).get_peer())
            padPeers["sink%d" % sinkPadNumber] = sw.get_pad("sink%d" % (
                sinkPadNumber)).get_peer().get_parent().get_name()

        for feedId in self.eater_names:
            eaterName = self.get_eater_name_for_feed_id(feedId)
            self.debug("feedId %s is mapped to eater name %s", feedId, 
                eaterName)
            if eaterName:
                for sinkPad in padPeers:
                    if feedId in padPeers[sinkPad]:
                        self.switchPads[eaterName] = sinkPad
                if not self.switchPads.has_key(eaterName):    
                    self.warning("could not find sink pad for eater %s", 
                        eaterName )
        # make sure switch has the correct sink pad as active
        self.debug("Setting switch's active-pad to %s", 
            self.switchPads[self._idealEater])
        self.switchElement.set_property("active-pad", 
            self.switchPads[self._idealEater])
        self.uiState.set("active-eater", self._idealEater)

    def switch_to(self, eater):
        if not self.switchElement:
            self.warning("switch_to called with eater %s but before pipeline "
                "configured")
            return False
        if not eater in [ "backup", "master" ]:
            self.warning ("%s is not master or backup", eater)
            return False
        if self.is_active(eater):
            self.switchElement.set_property("active-pad",
                self.switchPads[eater])
            self.uiState.set("active-eater", eater)
            return True
        else:
            self.warning("Could not switch to %s because the %s eater "
                "is not active." % (eater, eater))
        return False

    def eaterSetActive(self, feedId):
        Switch.eaterSetActive(self, feedId)
        eaterName = self.get_eater_name_for_feed_id(feedId)
        d = self._eaterReadyDefers[eaterName]
        if d:
            d.callback(True)
        self._eaterReadyDefers[eaterName] = None

class AVSwitch(Switch):
    logCategory = "comb-av-switch"

    def init(self):
        Switch.init(self)
        self.audioSwitchElement = None
        self.videoSwitchElement = None
        # eater name -> name of sink pad on switch element
        self.switchPads = {}
        self._startTimes = {}
        self._startTimeProbeIds = {}
        self._padProbeLock = threading.Lock()
        self._switchLock = threading.Lock()
        self.pads_awaiting_block = []
        self.padsBlockedDefer = None

    def do_check(self):
        d = Switch.do_check(self)
        def checkConfig(result):
            self.debug("checking config")
            props = self.config['properties']
            videoParams = {}
            audioParams = {}
            videoParams["video-width"] = props.get("video-width", None)
            videoParams["video-height"] = props.get("video-height", None)
            videoParams["video-framerate"] = props.get("video-framerate", None)
            videoParams["video-pixel-aspect-ratio"] = props.get("video-pixel-aspect-ratio", None)
            audioParams["audio-channels"] = props.get("audio-channels", None)
            audioParams["audio-samplerate"] = props.get("audio-samplerate", None)

            nonExistantVideoParams = []
            existsVideoParam = False
            allVideoParams = True
            for p in videoParams:
                if videoParams[p] == None:
                    allVideoParams = False
                    nonExistantVideoParams.append(p)
                else:
                    existsVideoParam = True
            self.debug("exists video param: %d all: %d nonexistant: %r", 
                existsVideoParam, allVideoParams, nonExistantVideoParams)
            if not allVideoParams and existsVideoParam:
                # message
                m = messages.Error(T_(N_(
                    "Video parameter(s) were specified but not all. "
                    "Missing parameters are: %r" % nonExistantVideoParams)),
                    id="video-params-not-specified")
                self.addMessage(m)
            nonExistantAudioParams = []
            existsAudioParam = False
            allAudioParams = True
            for p in audioParams:
                if audioParams[p] == None:
                    allAudioParams = False
                    nonExistantAudioParams.append(p)
                else:
                    existsAudioParam = True
            if not allAudioParams and existsAudioParam:
                # message
                m = messages.Error(T_(N_(
                    "Audio parameter(s) were specified but not all. "
                    "Missing parameters are: %r" % nonExistantAudioParams)),
                    id="audio-params-not-specified")
                self.addMessage(m)
            return result
        d.addCallback(checkConfig)
        return d

    def get_pipeline_string(self, properties):
        eaters = self.eater_names
        videoForceCapsTemplate = ""
        audioForceCapsTemplate = ""
        if properties.get("video-width", None):
            width = properties["video-width"]
            height = properties["video-height"]
            par = properties["video-pixel-aspect-ratio"]
            framerate = properties["video-framerate"]
            videoForceCapsTemplate = \
                "ffmpegcolorspace ! videorate ! videoscale !" \
                " capsfilter caps=video/x-raw-yuv,width=%d,height=%d," \
                "framerate=%d/%d,pixel-aspect-ratio=%d/%d," \
                "format=(fourcc)I420 " \
                "name=capsfilter-%%(eaterName)s ! " % (width, 
                height, framerate[0], framerate[1], par[0], par[1])
        if self.config.get("audio-channels", None):
            channels = self.config["audio-channels"]
            samplerate = self.config["audio-samplerate"]
            audioForceCapsTemplate = \
                "audioconvert ! audioconvert ! capsfilter caps=" \
                "audio/x-raw-int,channels=%d,samplerate=%d," \
                "width=16,depth=16,signed=true " \
                "name=capsfilter-%%(eaterName)s ! " % (
                channels, samplerate)
        pipeline = "switch name=vswitch ! " \
            "identity silent=true single-segment=true name=viden " \
            "switch name=aswitch ! " \
            "identity silent=true single-segment=true name=aiden "
        for eater in eaters:
            if "video" in eater:
                tmpl = '@ eater:%%(eaterName)s @ ! %s vswitch. ' % videoForceCapsTemplate
            if "audio" in eater:
                tmpl = '@ eater:%%(eaterName)s @ ! %s aswitch. ' % audioForceCapsTemplate
            pipeline += tmpl % dict(eaterName=eater)

        pipeline += 'viden. ! @feeder::video@ aiden. ! @feeder::audio@'
        return pipeline

    def configure_pipeline(self, pipeline, properties):
        self.videoSwitchElement = vsw = pipeline.get_by_name("vswitch")
        self.audioSwitchElement = asw = pipeline.get_by_name("aswitch")

        # figure out how many pads should be connected for the eaters
        # 1 + number of eaters with eaterName *-backup
        numVideoPads = 1 + len(self.config["eater"]["video-backup"])
        numAudioPads = 1 + len(self.config["eater"]["audio-backup"]) 
        padPeers = {} # (padName, switchElement) -> peer element name
        for sinkPadNumber in range(0, numVideoPads):
            padPeers[("sink%d" % sinkPadNumber, vsw)] = \
                vsw.get_pad("sink%d" % (
                sinkPadNumber)).get_peer().get_parent().get_name()
        for sinkPadNumber in range(0, numAudioPads):
            padPeers[("sink%d" % sinkPadNumber, asw)] = \
                asw.get_pad("sink%d" % (
                sinkPadNumber)).get_peer().get_parent().get_name()

        for feedId in self.eater_names:
            eaterName = self.get_eater_name_for_feed_id(feedId)
            self.debug("feedId %s is mapped to eater name %s", feedId, 
                eaterName)
            if eaterName:
                for sinkPadName, switchElement in padPeers:
                    if feedId in padPeers[(sinkPadName, switchElement)]:
                        self.switchPads[eaterName] = sinkPadName
                if not self.switchPads.has_key(eaterName):
                    self.warning("could not find sink pad for eater %s", 
                        eaterName )
        # make sure switch has the correct sink pad as active
        self.debug("Setting video switch's active-pad to %s", 
            self.switchPads["video-%s" % self._idealEater])
        vsw.set_property("active-pad", 
            self.switchPads["video-%s" % self._idealEater])
        self.debug("Setting audio switch's active-pad to %s",
            self.switchPads["audio-%s" % self._idealEater])
        asw.set_property("active-pad",
            self.switchPads["audio-%s" % self._idealEater])
        self.uiState.set("active-eater", self._idealEater)
        self.debug("active-eater set to %s", self._idealEater)

    # So switching audio and video is not that easy
    # We have to make sure the current segment on both
    # the audio and video switch element have the same
    # stop value, and the next segment on both to have
    # the same start value to maintain sync.
    # In order to do this:
    # 1) we need to block all src pads of elements connected 
    #    to the switches' sink pads
    # 2) we need to set the property "stop-value" on both the
    #    switches to the highest value of "last-timestamp" on the two
    #    switches.
    # 3) the pads should be switched (ie active-pad set) on the two switched
    # 4) the switch elements should be told to queue buffers coming on their
    #    active sinkpads by setting the queue-buffers property to TRUE
    # 5) pad buffer probes should be added to the now active sink pads of the 
    #    switch elements, so that the start value of the enxt new segment can
    #    be determined
    # 6) the src pads we blocked in 1) should be unblocked
    # 7) when both pad probes have fired once, use the timestamps received
    #    as the start value for the respective switch elements
    #    elements
    # 8) set the queue-buffers property on the switch elements to FALSE 
    def switch_to(self, eater):
        if not (self.videoSwitchElement and self.audioSwitchElement):
            self.warning("switch_to called with eater %s but before pipeline "
                "configured")
            return False
        if eater not in [ "master", "backup" ]:
            self.warning("%s is not master or backup", eater)
            return False
        if self._switchLock.locked():
            self.warning("Told to switch to %s while a current switch is going on.", eater)
            return False
        # Lock is acquired here and released once buffers are told to queue again
        self._switchLock.acquire()
        if self.is_active(eater) and self._startTimes == {} and \
           self.uiState.get("active-eater") != eater:
            self._startTimes = {"abc":None}
            self.padsBlockedDefer = defer.Deferred()
            self.debug("eaterSwitchingTo switching to %s", eater)
            self.eaterSwitchingTo = eater
            self._block_switch_sink_pads(True)
            return self.padsBlockedDefer
        else:
            self._switchLock.release()
            if self.uiState.get("active-eater") == eater:
                self.warning("Could not switch to %s because it is already active",
                    eater)
            elif self._startTimes == {}:
                self.warning("Could not switch to %s because at least "
                    "one of the %s eaters is not active." % (eater, eater))
                m = messages.Warning(T_(N_(
                    "Could not switch to %s because at least "
                    "one of the %s eaters is not active." % (eater, eater))),
                    id="cannot-switch",
                    priority=40)
                self.state.append('messages', m)
            else:
                self.warning("Could not switch because startTimes is %r",
                    self._startTimes)
                m = messages.Warning(T_(N_(
                    "Could not switch to %s because "
                    "startTimes is %r." % (eater, self._startTimes))),
                    id="cannot-switch",
                    priority=40)
                self.state.append('messages', m)
        return False
    
    def _set_last_timestamp(self):
        vswTs = self.videoSwitchElement.get_property("last-timestamp")
        aswTs = self.audioSwitchElement.get_property("last-timestamp")
        tsToSet = vswTs
        if aswTs > vswTs:
            tsToSet = aswTs
        self.log("Setting stop-value on video switch to %u",
            tsToSet)
        self.log("Setting stop-value on audio switch to %u",
            tsToSet)
        self.videoSwitchElement.set_property("stop-value",
            tsToSet)
        self.audioSwitchElement.set_property("stop-value",
            tsToSet)
        message = None
        if (aswTs > vswTs) and (aswTs - vswTs > gst.SECOND * 10):
            message = "When switching to %s the other source's video" \
                " and audio timestamps differ by %u" % (self.eaterSwitchingTo,
                aswTs - vswTs)
        elif (vswTs > aswTs) and (vswTs - aswTs > gst.SECOND * 10):
            message = "When switching to %s the other source's video" \
                " and audio timestamps differ by %u" % (self.eaterSwitchingTo,
                vswTs - aswTs)
        if message:
            m = messages.Warning(T_(N_(
                message)),
                id="large-timestamp-difference",
                priority=40)
            self.state.append('messages', m)

    def _block_cb(self, pad, blocked):
        self.log("here with pad %r and blocked %d", pad, blocked)
        if blocked:
            if not pad in self.pads_awaiting_block:
                return
            self.pads_awaiting_block.remove(pad)
            self.log("Pads awaiting block are: %r", self.pads_awaiting_block)
            #if not self.pads_awaiting_block:
            #    s = gst.Structure('pads-blocked')
            #    m = gst.message_new_application(self.pipeline, s)
                # marshal to the main thread
            #    self.pipeline.post_message(m)

    def _block_switch_sink_pads(self, block):
        if block:
            self.pads_awaiting_block = []
            for eaterName in self.switchPads:
                if "audio" in eaterName:
                    pad = self.audioSwitchElement.get_pad(
                        self.switchPads[eaterName]).get_peer()
                else:
                    pad = self.videoSwitchElement.get_pad(
                        self.switchPads[eaterName]).get_peer()
                if pad:
                    self.pads_awaiting_block.append(pad)
        
        for eaterName in self.switchPads:
            if "audio" in eaterName:
                pad = self.audioSwitchElement.get_pad(
                    self.switchPads[eaterName]).get_peer()
            else:
                pad = self.videoSwitchElement.get_pad(
                    self.switchPads[eaterName]).get_peer()
            if pad:
                self.debug("Pad: %r blocked being set to: %d", pad, block)
                ret = pad.set_blocked_async(block, self._block_cb)
                self.debug("Return of pad block is: %d", ret)
                self.debug("Pad %r is blocked: %d", pad, pad.is_blocked())
        if block:
            self.on_pads_blocked()

    def on_pads_blocked(self):
        eater = self.eaterSwitchingTo
        if not eater:
            self.warning("Eaterswitchingto is None, crying time")
        self.log("Block callback")
        self._set_last_timestamp()
        self.videoSwitchElement.set_property("active-pad",
        self.switchPads["video-%s" % eater])
        self.audioSwitchElement.set_property("active-pad",
        self.switchPads["audio-%s" % eater])
        self.videoSwitchElement.set_property("queue-buffers",
            True)
        self.audioSwitchElement.set_property("queue-buffers",
            True)
        self.uiState.set("active-eater", eater)
        self._add_pad_probes_for_start_time(eater)
        self._block_switch_sink_pads(False)
        if self.padsBlockedDefer:
            self.padsBlockedDefer.callback(True)
        else:
            self.warning("Our pad block defer is None, inconsistency time to cry")
        self.padsBlockedDefer = None

    def _add_pad_probes_for_start_time(self, activeEater):
        self.debug("adding buffer probes here for %s", activeEater)
        for eaterName in ["video-%s" % activeEater, "audio-%s" % activeEater]:
            if "audio" in eaterName:
                pad = self.audioSwitchElement.get_pad(
                    self.switchPads[eaterName])
            else:
                pad = self.videoSwitchElement.get_pad(
                    self.switchPads[eaterName])
            self._padProbeLock.acquire()
            self._startTimeProbeIds[eaterName] = pad.add_buffer_probe(
                self._start_time_buffer_probe, eaterName)
            self._padProbeLock.release()

    def _start_time_buffer_probe(self, pad, buffer, eaterName):
        self.debug("start time buffer probe for %s buf ts: %u", 
            eaterName, buffer.timestamp)
        self._padProbeLock.acquire()
        if eaterName in self._startTimeProbeIds:
            self._startTimes[eaterName] = buffer.timestamp
            pad.remove_buffer_probe(self._startTimeProbeIds[eaterName])
            del self._startTimeProbeIds[eaterName]
            self.debug("pad probe for %s", eaterName)
            self._check_start_times_received()
        self._padProbeLock.release()
        return True

    def _check_start_times_received(self):
        self.debug("here")
        activeEater = self.uiState.get("active-eater")
        haveAllStartTimes = True
        for eaterName in ["video-%s" % activeEater, "audio-%s" % activeEater]:
            haveAllStartTimes = haveAllStartTimes and \
                (eaterName in self._startTimes)
                
        if haveAllStartTimes:
            self.debug("have all start times")
            for eaterName in ["video-%s" % activeEater, "audio-%s" % activeEater]:
                if "video" in eaterName:
                    self.videoSwitchElement.set_property("start-value",
                        self._startTimes[eaterName])
                elif "audio" in eaterName:
                    self.audioSwitchElement.set_property("start-value",
                        self._startTimes[eaterName])
            self._startTimes = {}
            # we can also turn off the queue-buffers property
            self.audioSwitchElement.set_property("queue-buffers", False)
            self.videoSwitchElement.set_property("queue-buffers", False)
            self.log("eaterSwitchingTo becoming None from %s", 
                self.eaterSwitchingTo)
            self.eaterSwitchingTo = None
            self._switchLock.release()

    def eaterSetActive(self, feedId):
        Switch.eaterSetActive(self, feedId)
        eaterName = self.get_eater_name_for_feed_id(feedId)
        d = None
        if "master" in eaterName and self.is_active("master"):
            d = self._eaterReadyDefers["master"]
            self._eaterReadyDefers["master"] = None
        elif "backup" in eaterName and self.is_active("backup"):
            d = self._eaterReadyDefers["backup"]
            self._eaterReadyDefers["backup"] = None
        if d:
            d.callback(True)

