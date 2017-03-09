=========
dutch-boy
=========

.. image:: https://circleci.com/gh/Nextdoor/dutch-boy.svg?style=svg
    :target: https://circleci.com/gh/Nextdoor/dutch-boy

Mock and memory leak detector library.  Right now this just features a plugin
for Nose 1.x and is in **beta**.

This plugin:
  - Detects mocks that are not reset between tests.
  - Detects mocks created during tests that are not deleted by the end of the test.
  - Reports memory delta between tests.  Optional.

To learn the story of the name, visit `<https://en.wikipedia
.org/wiki/Hans_Brinker,_or_The_Silver_Skates>`_.

Pull requests are welcome.  In particular, area I'm looking to improve are:
  - adding a pytest test runner
  - improving memory delta reporting

============
Django Setup
============

To use this plugin with django nose runner, just include the following in your settings file::

    NOSE_PLUGINS = [
        'dutch_boy.nose.LeakDetectorPlugin']
    NOSE_ARGS = ['--with-leak-detector', '--leak-detector-level=3']

