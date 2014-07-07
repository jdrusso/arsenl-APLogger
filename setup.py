try:
  from setuptools import setup, find_packages
except ImportError:
  import distribute_setup
  distribute_setup.use_setuptools()
  from setuptools import setup, find_packages

setup(
    name='APLogger',
    install_requires=['nose==1.3.3'],
    py_modules=['aplogger'],
    entry_points={
      'nose.plugins.0.10': [
        'aplogger = aplogger:APLogger'
      ]
    }
)