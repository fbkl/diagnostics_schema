from distutils.core import setup
from catkin_pkg.python_setup import generate_distutils_setup
  
d = generate_distutils_setup(
    packages=['diagnostics_schema'],
    package_dir={'': 'src'},
    ## since we installed them we don't need to put the scripts here, they need sudo priviliges anyway, so it wouldn't work
    scripts=[]
)

setup(**d)

