# This file is part of Buildbot.  Buildbot is free software: you can
# redistribute it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, version 2.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc., 51
# Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
# Copyright Buildbot Team Members


import types

from twisted.internet import defer
from twisted.internet import error
from twisted.python import components
from twisted.python import log
from twisted.python.failure import Failure
from zope.interface import implements

from buildbot import interfaces
from buildbot.process import metrics
from buildbot.process import properties
from buildbot.status.builder import Results
from buildbot.status.progress import BuildProgress
from buildbot.status.results import EXCEPTION
from buildbot.status.results import FAILURE
from buildbot.status.results import RETRY
from buildbot.status.results import SKIPPED
from buildbot.status.results import SUCCESS
from buildbot.status.results import WARNINGS
from buildbot.status.results import worst_status
from buildbot.util.eventual import eventually

class BuildFailed(Exception):
    pass

class BuildStop(Exception):
    pass

class Build(properties.PropertiesMixin):

    """I represent a single build by a single slave. Specialized Builders can
    use subclasses of Build to hold status information unique to those build
    processes.

    I control B{how} the build proceeds. The actual build is broken up into a
    series of steps, saved in the .buildSteps[] array as a list of
    L{buildbot.process.step.BuildStep} objects. Each step is a single remote
    command, possibly a shell command.

    During the build, I put status information into my C{BuildStatus}
    gatherer.

    After the build, I go away.

    I can be used by a factory by setting buildClass on
    L{buildbot.process.factory.BuildFactory}

    @ivar requests: the list of L{BuildRequest}s that triggered me
    @ivar build_status: the L{buildbot.status.build.BuildStatus} that
                        collects our status
    """

    implements(interfaces.IBuildControl)

    workdir = "build"
    build_status = None
    reason = "changes"
    finished = False
    results = None
    stopped = False
    set_runtime_properties = True

    def __init__(self, requests):
        self.requests = requests
        self.locks = []
        # build a source stamp
        self.sources = requests[0].mergeSourceStampsWith(requests[1:])
        self.reason = requests[0].mergeReasons(requests[1:])

        self.progress = None
        self.currentSteps = []
        self.slaveEnvironment = {}

        self.terminate = False

        self._acquiringLock = None

    @property
    def currentStep(self):
        assert False, "Build: .currentStep is not available, use .currentSteps"

    def setBuilder(self, builder):
        """
        Set the given builder as our builder.

        @type  builder: L{buildbot.process.builder.Builder}
        """
        self.builder = builder
        self.master = builder.master

    def setLocks(self, lockList):
        # convert all locks into their real forms
        self.locks = [(self.builder.botmaster.getLockFromLockAccess(access), access)
                      for access in lockList]

    def setSlaveEnvironment(self, env):
        # TODO: remove once we don't have anything depending on this method or attribute
        self.slaveEnvironment = env

    def getSourceStamp(self, codebase=''):
        for source in self.sources:
            if source.codebase == codebase:
                return source
        return None

    def getAllSourceStamps(self):
        return list(self.sources)

    def allChanges(self):
        for s in self.sources:
            for c in s.changes:
                yield c

    def allFiles(self):
        # return a list of all source files that were changed
        files = []
        for c in self.allChanges():
            for f in c.files:
                files.append(f)
        return files

    def __repr__(self):
        return "<Build %s>" % (self.builder.name,)

    def blamelist(self):
        blamelist = []
        for c in self.allChanges():
            if c.who not in blamelist:
                blamelist.append(c.who)
        for source in self.sources:
            if source.patch_info:  # Add patch author to blamelist
                blamelist.append(source.patch_info[0])
        blamelist.sort()
        return blamelist

    def changesText(self):
        changetext = ""
        for c in self.allChanges():
            changetext += "-" * 60 + "\n\n" + c.asText() + "\n"
        # consider sorting these by number
        return changetext

    def setStepFactories(self, step_factories):
        """Set a list of 'step factories', which are tuples of (class,
        kwargs), where 'class' is generally a subclass of step.BuildStep .
        These are used to create the Steps themselves when the Build starts
        (as opposed to when it is first created). By creating the steps
        later, their __init__ method will have access to things like
        build.allFiles() ."""
        self.stepFactories = list(step_factories)

    useProgress = True

    def getSlaveCommandVersion(self, command, oldversion=None):
        return self.slavebuilder.getSlaveCommandVersion(command, oldversion)

    def getSlaveName(self):
        return self.slavebuilder.slave.slavename

    def setupProperties(self):
        props = interfaces.IProperties(self)

        # give the properties a reference back to this build
        props.build = self

        # start with global properties from the configuration
        master = self.builder.master
        props.updateFromProperties(master.config.properties)

        # from the SourceStamps, which have properties via Change
        for change in self.allChanges():
            props.updateFromProperties(change.properties)

        # and finally, get any properties from requests (this is the path
        # through which schedulers will send us properties)
        for rq in self.requests:
            props.updateFromProperties(rq.properties)

        # now set some properties of our own, corresponding to the
        # build itself
        props.setProperty("buildnumber", self.build_status.number, "Build")

        if self.sources and len(self.sources) == 1:
            # old interface for backwards compatibility
            source = self.sources[0]
            props.setProperty("branch", source.branch, "Build")
            props.setProperty("revision", source.revision, "Build")
            props.setProperty("repository", source.repository, "Build")
            props.setProperty("codebase", source.codebase, "Build")
            props.setProperty("project", source.project, "Build")

        self.builder.setupProperties(props)

    def setupSlaveBuilder(self, slavebuilder):
        self.slavebuilder = slavebuilder

        self.path_module = slavebuilder.slave.path_module

        # navigate our way back to the L{buildbot.buildslave.BuildSlave}
        # object that came from the config, and get its properties
        buildslave_properties = slavebuilder.slave.properties
        self.getProperties().updateFromProperties(buildslave_properties)
        if slavebuilder.slave.slave_basedir:
            builddir = self.path_module.join(
                slavebuilder.slave.slave_basedir,
                self.builder.config.slavebuilddir)
            self.setProperty("builddir", builddir, "slave")
            self.setProperty("workdir", builddir, "slave (deprecated)")

        self.slavename = slavebuilder.slave.slavename
        self.build_status.setSlavename(self.slavename)

    def startBuild(self, build_status, expectations, slavebuilder):
        """This method sets up the build, then starts it by invoking the
        first Step. It returns a Deferred which will fire when the build
        finishes. This Deferred is guaranteed to never errback."""

        # we are taking responsibility for watching the connection to the
        # remote. This responsibility was held by the Builder until our
        # startBuild was called, and will not return to them until we fire
        # the Deferred returned by this method.

        log.msg("%s.startBuild" % self)
        self.build_status = build_status
        # now that we have a build_status, we can set properties
        self.setupProperties()
        self.setupSlaveBuilder(slavebuilder)
        slavebuilder.slave.updateSlaveStatus(buildStarted=build_status)

        # then narrow SlaveLocks down to the right slave
        self.locks = [(l.getLock(self.slavebuilder.slave), a)
                      for l, a in self.locks]
        self.remote = slavebuilder.remote
        self.remote.notifyOnDisconnect(self.lostRemote)

        metrics.MetricCountEvent.log('active_builds', 1)

        d = self.deferred = defer.Deferred()

        def _uncount_build(res):
            metrics.MetricCountEvent.log('active_builds', -1)
            return res
        d.addBoth(_uncount_build)

        def _release_slave(res, slave, bs):
            self.slavebuilder.buildFinished()
            slave.updateSlaveStatus(buildFinished=bs)
            return res
        d.addCallback(_release_slave, self.slavebuilder.slave, build_status)

        try:
            self.setupBuild(expectations)  # create .steps
        except:
            # the build hasn't started yet, so log the exception as a point
            # event instead of flunking the build.
            # TODO: associate this failure with the build instead.
            # this involves doing
            # self.build_status.buildStarted() from within the exception
            # handler
            log.msg("Build.setupBuild failed")
            log.err(Failure())
            self.builder.builder_status.addPointEvent(["setupBuild",
                                                       "exception"])
            self.finished = True
            self.results = EXCEPTION
            self.deferred = None
            d.callback(self)
            return d

        self.build_status.buildStarted(self)
        self.acquireLocks().addCallback(self.lauchJob)
        return d

    @staticmethod
    def canStartWithSlavebuilder(lockList, slavebuilder):
        for lock, access in lockList:
            slave_lock = lock.getLock(slavebuilder.slave)
            if not slave_lock.isAvailable(None, access):
                return False
        return True

    def acquireLocks(self, res=None):
        self._acquiringLock = None
        if not self.locks:
            return defer.succeed(None)
        if self.stopped:
            return defer.succeed(None)
        log.msg("acquireLocks(build %s, locks %s)" % (self, self.locks))
        for lock, access in self.locks:
            if not lock.isAvailable(self, access):
                log.msg("Build %s waiting for lock %s" % (self, lock))
                d = lock.waitUntilMaybeAvailable(self, access)
                d.addCallback(self.acquireLocks)
                self._acquiringLock = (lock, access, d)
                return d
        # all locks are available, claim them all
        for lock, access in self.locks:
            lock.claim(self, access)
        return defer.succeed(None)

    def addStep(self, step, insertPosition=0, addToQueue=True):
        assert step._step_status is None, "Step was already used. Always create new instance!"

        step.setBuild(self)
        step.setBuildSlave(self.slavebuilder.slave)
        # TODO: remove once we don't have anything depending on setDefaultWorkdir
        if callable(self.workdir):
            step.setDefaultWorkdir(self.workdir(self.sources))
        else:
            step.setDefaultWorkdir(self.workdir)
        name = step.name
        if name in self.stepnames:
            count = self.stepnames[name]
            count += 1
            self.stepnames[name] = count
            name = step.name + "_%d" % count
        else:
            self.stepnames[name] = 0
        step.name = name
        buildStatusInsertPosition = None
        if insertPosition is None:
            index = len(self.steps)
        elif isinstance(insertPosition, int):
            index = insertPosition
            if index < len(self.steps):
                buildStatusInsertPosition = self.steps[index]._step_status.step_number
        else:
            try:
                buildStatusInsertPosition = insertPosition._step_status
                index = self.steps.index(insertPosition)
                index += 1
            except ValueError:
                index = 0
                if len(self.steps) > 0 and buildStatusInsertPosition is None:
                    buildStatusInsertPosition = self.steps[0]._step_status.step_number
        if addToQueue:
            self.steps.insert(index, step)

        # tell the BuildStatus about the step. This will create a
        # BuildStepStatus and bind it to the Step.
        step_status = self.build_status.addStepWithName(name, buildStatusInsertPosition)
        step.setStepStatus(step_status)

        sp = None
        if self.useProgress:
            # XXX: maybe bail if step.progressMetrics is empty? or skip
            # progress for that one step (i.e. "it is fast"), or have a
            # separate "variable" flag that makes us bail on progress
            # tracking
            sp = step.setupProgress()
        if sp:
            self.progress.addStepProgress(sp)

        return step

    def setupBuild(self, expectations):
        # create the actual BuildSteps. If there are any name collisions, we
        # add a count to the loser until it is unique.
        self.steps = []
        self.stepStatuses = {}
        self.stepnames = {}

        if self.useProgress:
            self.progress = BuildProgress()

        for factory in self.stepFactories:
            step = factory.buildStep()
            self.addStep(step, None)

        # Create a buildbot.status.progress.BuildProgress object. This is
        # called once at startup to figure out how to build the long-term
        # Expectations object, and again at the start of each build to get a
        # fresh BuildProgress object to track progress for that individual
        # build. TODO: revisit at-startup call

        if self.progress and expectations:
            self.progress.setExpectationsFrom(expectations)

        # we are now ready to set up our BuildStatus.
        # pass all sourcestamps to the buildstatus
        self.build_status.setSourceStamps(self.sources)
        self.build_status.setReason(self.reason)
        self.build_status.setBlamelist(self.blamelist())
        self.build_status.setProgress(self.progress)

        # gather owners from build requests
        owners = [r.properties['owner'] for r in self.requests
                  if "owner" in r.properties]
        if owners:
            self.setProperty('owners', owners, self.reason)

        self.results = []  # list of FAILURE, SUCCESS, WARNINGS, SKIPPED
        self.result = SUCCESS  # overall result, may downgrade after each step
        self.text = []  # list of text string lists (text2)

    def lauchJob(self, *args, **kwargs):
        # Job will live asyncroniously
        self.doJob_()

    @defer.inlineCallbacks
    def doJob_(self):
        try:
            try:
                yield self.run()
                yield self.runCleanup()
            except BuildFailed as e:
                yield self.runCleanup()
            yield self.runExistedSteps()
            yield self.allStepsDone()
        except BuildStop as e:
            yield self.allStepsDone()
        except Exception as e:
            log.err()
            yield self.buildException(e)

    def run(self):
        pass

    def runCleanup(self):
        pass

    @defer.inlineCallbacks
    def runExistedSteps(self):
        while True:
            s = self.getNextStep()
            if not s:
                break
            yield self._processStep(s)

    @defer.inlineCallbacks
    def processStep(self, step):
        if len(self.currentSteps) > 0:
            assert len(self.currentSteps) == 0
        if step._step_status is None:
            yield self.addStep(step, insertPosition=0, addToQueue=False)
        isTerminateStage = self.terminate
        yield self._processStep(step)
        if not isTerminateStage and self.terminate:
            raise BuildFailed()

    @defer.inlineCallbacks
    def processStepsInParallel(self, steps, maxCount=2):
        assert len(self.currentSteps) == 0
        for step in steps:
            if step._step_status is None:
                yield self.addStep(step, insertPosition=None, addToQueue=True)
            elif not step in self.steps:
                self.steps.append(step)

        build = self

        class ParallelStepsProcessor():

            def __init__(self):
                self.launched = []
                self.launched_deferred_results = []

            def inProgress(self):
                return len(self.launched)

            def launch(self, step):
                assert not step in build.currentSteps
                if build.stopped:
                    raise BuildStop()
                build.currentSteps.append(step)
                deferedResult = step.startStep(build.remote)
                self.launched.append(step)
                self.launched_deferred_results.append(deferedResult)

            @defer.inlineCallbacks
            def waitForOneStepCompletion(self):
                """Returns True on build termination"""
                results, index = yield defer.DeferredList(self.launched_deferred_results, fireOnOneCallback=True, fireOnOneErrback=True, consumeErrors=False)
                completedStep = self.launched[index]
                del self.launched[index]
                del self.launched_deferred_results[index]
                build.currentSteps.remove(completedStep)
                terminate = build.stepDone(results, completedStep)  # interpret/merge results
                yield completedStep.onCompletion(results)
                if terminate:
                    build.terminate = True
                defer.returnValue(terminate)

            def getNextStep(self):
                for s in build.steps:
                    if s.isReady():
                        build.steps.remove(s)
                        return s
                return len(build.steps) == 0

            def __enter__(self):
                return self

            def __exit__(self, type, value, tb):
                try:
                    while self.inProgress() > 0:
                        yield self.waitForOneStepCompletion()
                except:
                    log.err()
                for step in self.launched:
                    if step in build.currentSteps:
                        build.currentSteps.remove(step)


        with ParallelStepsProcessor() as parallelSteps:
            while True:
                step = parallelSteps.getNextStep()
                if step is True:
                    if parallelSteps.inProgress() == 0:
                        break
                elif step is not False:
                    parallelSteps.launch(step)

                if parallelSteps.inProgress() == maxCount or step in [True, False]:
                    if (yield parallelSteps.waitForOneStepCompletion()):
                        break

        if self.terminate:
            raise BuildFailed()

    @defer.inlineCallbacks
    def _processStep(self, step):
        assert not step in self.currentSteps
        if self.stopped:
            raise BuildStop()
        self.currentSteps.append(step)
        try:
            results = yield step.startStep(self.remote)
            terminate = self.stepDone(results, step)  # interpret/merge results
            yield step.onCompletion(results)
            if terminate:
                self.terminate = True
        finally:
            self.currentSteps.remove(step)

    def getNextStep(self):
        """This method is called to obtain the next BuildStep for this build.
        When it returns None (or raises a StopIteration exception), the build
        is complete."""
        if not self.steps:
            return None
        if not self.remote:
            return None
        if self.terminate or self.stopped:
            # Run any remaining alwaysRun steps, and skip over the others
            while True:
                s = self.steps.pop(0)
                assert s.isReady()
                if s.alwaysRun:
                    return s
                if not self.steps:
                    return None
        else:
            return self.steps.pop(0)

    def stepDone(self, result, step):
        """This method is called when the BuildStep completes. It is passed a
        status object from the BuildStep and is responsible for merging the
        Step's results into those of the overall Build."""

        terminate = False
        text = None
        if isinstance(result, types.TupleType):
            result, text = result
        assert isinstance(result, type(SUCCESS)), "got %r" % (result,)
        log.msg(" step '%s' complete: %s" % (step.name, Results[result]))
        self.results.append(result)
        if text:
            self.text.extend(text)
        if not self.remote:
            terminate = True

        step._completionResult = result

        possible_overall_result = result
        if result == FAILURE:
            if not step.flunkOnFailure:
                possible_overall_result = SUCCESS
            if step.warnOnFailure:
                possible_overall_result = WARNINGS
            if step.flunkOnFailure:
                possible_overall_result = FAILURE
            if step.haltOnFailure:
                terminate = True
        elif result == WARNINGS:
            if not step.warnOnWarnings:
                possible_overall_result = SUCCESS
            else:
                possible_overall_result = WARNINGS
            if step.flunkOnWarnings:
                possible_overall_result = FAILURE
        elif result in (EXCEPTION, RETRY):
            terminate = True

        # if we skipped this step, then don't adjust the build status
        if result != SKIPPED:
            self.result = worst_status(self.result, possible_overall_result)

        return terminate

    def lostRemote(self, remote=None):
        # the slave went away. There are several possible reasons for this,
        # and they aren't necessarily fatal. For now, kill the build, but
        # TODO: see if we can resume the build when it reconnects.
        log.msg("%s.lostRemote" % self)
        self.remote = None
        if len(self.currentSteps) > 0:
            for step in self.currentSteps:
                # this should cause the step to finish.
                log.msg(" stopping current step", step)
                step.interrupt(Failure(error.ConnectionLost()))
        else:
            self.result = RETRY
            self.text = ["lost", "remote"]
            self.stopped = True
            if self._acquiringLock:
                lock, access, d = self._acquiringLock
                lock.stopWaitingUntilAvailable(self, access, d)
                d.callback(None)

    def stopBuild(self, reason="<no reason given>"):
        # the idea here is to let the user cancel a build because, e.g.,
        # they realized they committed a bug and they don't want to waste
        # the time building something that they know will fail. Another
        # reason might be to abandon a stuck build. We want to mark the
        # build as failed quickly rather than waiting for the slave's
        # timeout to kill it on its own.

        log.msg(" %s: stopping build: %s" % (self, reason))
        if self.finished:
            return
        # TODO: include 'reason' in this point event
        self.builder.builder_status.addPointEvent(['interrupt'])
        self.stopped = True
        for step in self.currentSteps:
            step.interrupt(reason)

        self.result = EXCEPTION

        if self._acquiringLock:
            lock, access, d = self._acquiringLock
            lock.stopWaitingUntilAvailable(self, access, d)
            d.callback(None)

    def allStepsDone(self):
        if self.result == FAILURE:
            text = ["failed"]
        elif self.result == WARNINGS:
            text = ["warnings"]
        elif self.result == EXCEPTION:
            text = ["exception"]
        elif self.result == RETRY:
            text = ["retry"]
        else:
            text = ["build", "successful"]
        text.extend(self.text)
        return self.buildFinished(text, self.result)

    def buildException(self, why):
        log.msg("%s.buildException" % self)
        log.err(why)
        # try to finish the build, but since we've already faced an exception,
        # this may not work well.
        try:
            self.buildFinished(["build", "exception"], EXCEPTION)
        except:
            log.err(Failure(), 'while finishing a build with an exception')

    def buildFinished(self, text, results):
        """This method must be called when the last Step has completed. It
        marks the Build as complete and returns the Builder to the 'idle'
        state.

        It takes two arguments which describe the overall build status:
        text, results. 'results' is one of SUCCESS, WARNINGS, or FAILURE.

        If 'results' is SUCCESS or WARNINGS, we will permit any dependant
        builds to start. If it is 'FAILURE', those builds will be
        abandoned."""

        self.finished = True
        if self.remote:
            self.remote.dontNotifyOnDisconnect(self.lostRemote)
            self.remote = None
        self.results = results

        log.msg(" %s: build finished" % self)
        self.build_status.setText(text)
        self.build_status.setResults(results)
        self.build_status.buildFinished()
        if self.progress and results == SUCCESS:
            # XXX: also test a 'timing consistent' flag?
            log.msg(" setting expectations for next time")
            self.builder.setExpectations(self.progress)
        eventually(self.releaseLocks)
        self.deferred.callback(self)
        self.deferred = None

    def releaseLocks(self):
        if self.locks:
            log.msg("releaseLocks(%s): %s" % (self, self.locks))
        for lock, access in self.locks:
            if lock.isOwner(self, access):
                lock.release(self, access)
            else:
                # This should only happen if we've been interrupted
                assert self.stopped

    # IBuildControl

    def getStatus(self):
        return self.build_status

    # stopBuild is defined earlier

components.registerAdapter(
    lambda build: interfaces.IProperties(build.build_status),
    Build, interfaces.IProperties)
