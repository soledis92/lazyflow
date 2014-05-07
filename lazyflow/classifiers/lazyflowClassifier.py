import abc

def _has_attribute( cls, attr ):
    return any(attr in B.__dict__ for B in cls.__mro__)

def _has_attributes( cls, attrs ):
    return all(_has_attribute(cls, a) for a in attrs)

class LazyflowClassifierFactoryABC(object):
    __metaclass__ = abc.ABCMeta

    @abc.abstractmethod
    def create_and_train(self, X, y):
        raise NotImplementedError

    @abc.abstractproperty
    def description(self):
        raise NotImplementedError

    @classmethod
    def __subclasshook__(cls, C):
        if cls is LazyflowClassifierABC:
            return _has_attributes(C, ['create_and_train', 'description'])
        return NotImplemented

class LazyflowClassifierABC(object):
    """
    Defines an interface for classifier objects that can be used by the lazyflow classifier operators.
    All scikit-learn classifiers already satisfy this interface.
    """
    __metaclass__ = abc.ABCMeta

    @abc.abstractmethod
    def predict_probabilities(self, X):
        raise NotImplementedError

    @abc.abstractproperty
    def known_classes(self):
        raise NotImplementedError

    @classmethod
    def __subclasshook__(cls, C):
        if cls is LazyflowClassifierABC:
            return _has_attributes(C, ['predict_probabilities', 'known_classes'])
        return NotImplemented
